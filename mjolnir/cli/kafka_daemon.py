"""
Daemon to collect elasticsearch bulk requests from kafka,
run them against relforge, and send the results back over
kafka.
"""

import argparse
import mjolnir.kafka.daemon


def parse_arguments():
    parser = argparse.ArgumentParser(description='...')
    parser.add_argument(
        '-b', '--brokers', dest='brokers', required=True, type=lambda x: x.split(','),
        help='Kafka brokers to bootstrap from as a comma separated list of <host>:<port>')
    parser.add_argument(
        '-w', '--num-workers', dest='n_workers', type=int, default=5,
        help='Number of workers to issue elasticsearch queries in parallel. '
             + 'Defaults to 5.')
    args = parser.parse_args()
    return dict(vars(args))


if __name__ == '__main__':
    args = parse_arguments()
    mjolnir.kafka.daemon.Daemon(**args).run()