"""Fail fast if required AWS configuration for this app is missing."""

import os


def validate_required_aws_env():
    missing = []
    if not os.environ.get('AWS_SNS_TOPIC_ARN'):
        missing.append('AWS_SNS_TOPIC_ARN')
    if not os.environ.get('AWS_DYNAMODB_TABLE_NAME'):
        missing.append('AWS_DYNAMODB_TABLE_NAME')
    if not os.environ.get('AWS_DYNAMODB_SESSION_TABLE_NAME'):
        missing.append('AWS_DYNAMODB_SESSION_TABLE_NAME')
    if not (os.environ.get('AWS_S3_BUCKET_NAME') or os.environ.get('AWS_S3_BUCKET')):
        missing.append('AWS_S3_BUCKET_NAME (or AWS_S3_BUCKET)')
    if missing:
        raise RuntimeError(
            'Missing required AWS environment variables: '
            + ', '.join(missing)
            + '. Configure SNS, DynamoDB (ratings table + session table), and S3.'
        )
