"""
Example script demonstrating the training portion of the MLR pipeline.
This is mostly to demonstrate how everything ties together

To run:
    PYSPARK_PYTHON=venv/bin/python spark-submit \
        --jars /path/to/mjolnir-with-dependencies.jar
        --artifacts 'mjolnir_venv.zip#venv' \
        path/to/training_pipeline.py
"""

import argparse
import mjolnir.training.xgboost
import os
import pickle
from pyspark import SparkContext
from pyspark.sql import HiveContext
from pyspark.sql import functions as F


def main(sc, sqlContext, input_dir, output_dir, wikis, target_node_evaluations,
         num_workers, num_cv_jobs, num_folds):

    for wiki in wikis:
        print 'Training wiki: %s' % (wiki)
        df_hits_with_features = (
            sqlContext.read.parquet(input_dir)
            .where(F.col('wikiid') == wiki))

        data_size = df_hits_with_features.count()
        if data_size == 0:
            print 'No data found.' % (wiki)
            print ''
            continue

        # Make a guess at the number of fold partitions needed based on data size.
        # This requires there to be around 40k data points, arbitrarily chosen,
        # per partition used to calculate the folds, up to 100 partitions.
        num_fold_partitions = min(100, max(1, data_size / 40000))

        # Explore a hyperparameter space. Skip the most expensive part of tuning,
        # increasing the # of trees, with target_node_evaluations=None
        tune_results = mjolnir.training.xgboost.tune(
            df_hits_with_features, num_folds=num_folds, num_fold_partitions=num_fold_partitions,
            num_cv_jobs=num_cv_jobs, num_workers=num_workers,
            target_node_evaluations=target_node_evaluations)

        # Save the tune results somewhere for later analysis. Use pickle
        # to maintain the hyperopt.Trials objects as is.
        tune_output = os.path.join(output_dir, 'tune_%s.pickle' % (wiki))
        with open(tune_output, 'w') as f:
            f.write(pickle.dumps(tune_results))
            print 'Wrote tuning results to %s' % (tune_output)

        print 'CV  test-ndcg@10: %.4f' % (tune_results['metrics']['test'])
        print 'CV train-ndcg@10: %.4f' % (tune_results['metrics']['train'])

        # Train a model over all data with best params
        best_params = tune_results['params']
        print 'Best parameters:'
        for param, value in best_params.items():
            print '\t%20s: %s' % (param, value)
        df_grouped, j_groups = mjolnir.training.xgboost.prep_training(
            df_hits_with_features, num_workers)
        best_params['groupData'] = j_groups
        model = mjolnir.training.xgboost.train(df_grouped, best_params)

        print 'train-ndcg@10: %.3f' % (model.eval(df_grouped, j_groups))

        # Generate a feature map so xgboost can include feature names in the dump.
        # The final `q` indicates all features are quantitative values (floats).
        features = df_hits_with_features.schema['features'].metadata['features']
        feat_map = ["%d %s q" % (i, fname) for i, fname in enumerate(features)]
        json_model_output = os.path.join(output_dir, 'model_%s.json' % (wiki))
        with open(json_model_output, 'wb') as f:
            f.write(model.dump("\n".join(feat_map)))
            print 'Wrote xgboost json model to %s' % (json_model_output)
        # Write out the xgboost binary format as well, so it can be re-loaded
        # and evaluated
        xgb_model_output = os.path.join(output_dir, 'model_%s.xgb' % (wiki))
        model.saveModelAsLocalFile(xgb_model_output)
        print 'Wrote xgboost binary model to %s' % (xgb_model_output)
        print ''


def parse_arguments():
    parser = argparse.ArgumentParser(description='Train XGBoost ranking models')
    parser.add_argument(
        '-i', '--input', dest='input_dir', type=str, required=True,
        help='Input path, prefixed with hdfs://, to dataframe with labels and features')
    parser.add_argument(
        '-o', '--output', dest='output_dir', type=str, required=True,
        help='Path, on local filesystem, to directory to store the results of '
             'model training to.')
    parser.add_argument(
        '-w', '--workers', dest='num_workers', default=10, type=int,
        help='Number of workers to train each individual model with. The total number '
             + 'of executors required is workers * cv-jobs. (Default: 10)')
    parser.add_argument(
        '-c', '--cv-jobs', dest='num_cv_jobs', default=None, type=int,
        help='Number of cross validations to perform in parallel. Defaults to number '
             + 'of folds, to run all in parallel.')
    parser.add_argument(
        '-f', '--folds', dest='num_folds', default=5, type=int,
        help='Number of cross validation folds to use. (Default: 5)')
    parser.add_argument(
        '-n', '--node-evaluations', dest='target_node_evaluations', type=int, default=None,
        help='Approximate number of node evaluations per predication that '
             + 'the final result will require. This controls the number of '
             + 'trees used in the final result. Default uses 100 trees rather '
             + 'than dynamically choosing based on max_depth. (Default: None)')
    parser.add_argument(
        'wikis', metavar='wiki', type=str, nargs='+',
        help='A wiki to perform model training for.')

    args = parser.parse_args()
    if args.num_cv_jobs is None:
        args.num_cv_jobs = args.num_folds
    return dict(vars(args))


if __name__ == "__main__":
    args = parse_arguments()
    # TODO: Set spark configuration? Some can't actually be set here though, so best might be to set all of it
    # on the command line for consistency.
    sc = SparkContext(appName="MLR: training pipeline")
    sc.setLogLevel('WARN')
    sqlContext = HiveContext(sc)
    main(sc, sqlContext, **args)