"""
Example script demonstrating the full LTR pipeline. It may
not be desirable to run this all at once, but rather saving
intermediate stages to HDFS for later analysis. This is mostly
to demonstrate how everything ties together

To run:
    PYSPARK_PYTHON=MJOLNIR/venv/bin/python spark-submit \
        --jars hdfs://analytics-hadoop/wmf/refinery/current/artifacts/refinery-hive.jar \
        --artifacts 'mjolnir_venv.zip#MJOLNIR' \
        path/to/training_pipeline.py
"""

import mjolnir.dbn
import mjolnir.sampling
import mjolnir.features
import mjolnir.training
import mjolnir.training.xgboost
from pyspark import SparkContext
from pyspark.sql import HiveContext
from pyspark.sql import functions as F


def main(sc, sqlContext):
    sqlContext.sql("CREATE TEMPORARY FUNCTION stemmer AS 'org.wikimedia.analytics.refinery.hive.StemmerUDF'")

    # Load click data from HDFS
    df_clicks = (
        sqlContext.read.parquet(
            'hdfs://analytics-hadoop/wmf/data/discovery/query_clicks/daily/year=*/month=*/day=*')
        # Clicks and hits contains a bunch of useful debugging data, but we don't
        # need any of that here. Save a bunch of memory by only working with
        # lists of page ids
        .withColumn('hit_page_ids', F.col('hits.pageid'))
        .drop('hits')
        .withColumn('click_page_ids', F.col('clicks.pageid'))
        .drop('clicks')
        # Normalize queries using the lucene stemmer
        .withColumn('norm_query', F.expr('stemmer(query, substring(wikiid, 1, 2))')))

    # Sample to some subset of queries per wiki
    df_sampled = (
        mjolnir.sampling.sample(
            df_clicks,
            wikis=['enwiki', 'dewiki', 'ruwiki', 'frwiki'],
            seed=54321,
            queries_per_wiki=20000,
            min_sessions_per_query=10)
        # Explode source into a row per displayed hit, reduce to a row per
        # unique (wikiid, query, page_id) and add a count of the number of
        # duplicates removed.
        .select('*', F.expr("posexplode(hit_page_ids)").alias('hit_position', 'hit_page_id'))
        .drop('hit_page_ids')
        .withColumn('clicked', F.expr('array_contains(click_page_ids, hit_page_id)'))
        .drop('click_page_ids'))

    # Learn relevances
    df_rel = (
        mjolnir.dbn.train(df_sampled, {
            'MAX_ITERATIONS': 40,
            'DEBUG': False,
            'PRETTY_LOG': True,
            'MIN_DOCS_PER_QUERY': 10,
            'MAX_DOCS_PER_QUERY': 20,
            'SERP_SIZE': 20,
            'QUERY_INDEPENDENT_PAGER': False,
            'DEFAULT_REL': 0.5})
        # naive conversion of relevance % into a label
        .withColumn('label', (F.col('relevance') * 10).cast('int')))

    df_hits = (
        df_sampled
        .groupBy('wikiid', 'query', 'norm_query', 'hit_page_id')
        # weight is now the number of times a hit was displayed to a user
        .agg(F.count(F.lit(1)).alias('weight'))
        # Join in the relevance labels
        .join(df_rel, how='inner', on=['wikiid', 'norm_query', 'hit_page_id']))

    # Collect features for all known queries. Note that this intentionally
    # uses query and NOT norm_query. Merge those back into the source hits.
    df_features = mjolnir.features.collect(
        df_hits,
        url_list=['http://elastic%d.eqiad.wmnet:9200/_msearch' % (i) for i in range(1017, 1053)],
        indices={wiki: '%s_content' % (wiki) for wiki in ['enwiki', 'dewiki', 'frwiki', 'ruwiki']},
        feature_definitions=mjolnir.features.enwiki_features())
    df_hits_with_features = df_hits.join(df_features, how='inner', on=['wikiid', 'query', 'hit_page_id'])

    # Probably this should be written out to disk, then loaded from disk to run training.
    # You do not want to accidentally re-request all features from elasticsearch
    df_hits_with_features.cache()

    # Doesn't have to be done ahead of time, but if the data is used multiple times
    # (eg train and eval) then it should to save work.
    df_grouped, j_groups = mjolnir.training.xgboost.prep_training(
        df_hits_with_features, {'num_workers': 10})

    # Train a model
    # Note that this might be best done in a separate spark context, with different options.
    # All of the above code will use 1 cpu per task, but xgboost can use multiple cores per task.
    # To take advantage of multiple cores you should use spark.task.cpus=n in the spark config.
    model = mjolnir.training.xgboost.train(df_grouped, {
        'num_rounds': 100,
        'num_workers': 10,
        'objective': 'rank:ndcg',
        'eval_metric': 'ndcg@10',
        'eta': 0.3,
        'max_depth': 6,
        'groupData': j_groups,
    })

    print 'train-ndcg@10: %.3f' % (model.eval(df_grouped, j_groups))

    # TODO: Nothing below here is implemented, and is only included
    # as a rough estimate of what will be implemented.

    # Write out the model in a format suitable for loading into
    # the elasticsearch plugin
    model.write('/home/ebernhardson/xgboost_model.xml')


if __name__ == "__main__":
    sc = SparkContext(appName="LTR: training pipeline")
    sqlContext = HiveContext(sc)
    main(sc, sqlContext)
