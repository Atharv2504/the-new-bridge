import io
import json
import os
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError


def get_region():
    return (
        os.environ.get('AWS_DEFAULT_REGION')
        or os.environ.get('AWS_REGION')
        or 'us-east-1'
    )


def _sns_topic_arn():
    arn = os.environ.get('AWS_SNS_TOPIC_ARN')
    if not arn:
        raise RuntimeError('AWS_SNS_TOPIC_ARN is required.')
    return arn


def _ratings_table():
    name = os.environ.get('AWS_DYNAMODB_TABLE_NAME')
    if not name:
        raise RuntimeError('AWS_DYNAMODB_TABLE_NAME is required.')
    return boto3.resource('dynamodb', region_name=get_region()).Table(name)


def get_s3_bucket_name():
    return os.environ.get('AWS_S3_BUCKET_NAME') or os.environ.get('AWS_S3_BUCKET')


def upload_verification_docs(file_obj, user_id):
    """
    Uploads a document to Amazon S3 and optionally scans it with Rekognition.
    """
    bucket_name = get_s3_bucket_name()
    if hasattr(file_obj, 'seek'):
        try:
            file_obj.seek(0)
        except (OSError, io.UnsupportedOperation):
            pass
    if not bucket_name:
        raise RuntimeError('AWS_S3_BUCKET_NAME (or AWS_S3_BUCKET) must be set for document uploads.')

    s3 = boto3.client('s3', region_name=get_region())
    rekognition = boto3.client('rekognition', region_name=get_region())

    file_name = file_obj.filename
    s3_key = f'user_{user_id}/{file_name}'

    print(f"\n[AWS S3] Uploading '{file_name}' to {bucket_name}...")
    s3.upload_fileobj(file_obj, bucket_name, s3_key)
    s3_link = f's3://{bucket_name}/{s3_key}'
    print(f'[AWS S3] Upload successful: {s3_link}')

    print('[AWS REKOGNITION] Scanning document for labels...')
    try:
        response = rekognition.detect_labels(
            Image={'S3Object': {'Bucket': bucket_name, 'Name': s3_key}},
            MaxLabels=5,
        )
        labels = [label['Name'] for label in response['Labels']]
        print(f'[AWS REKOGNITION] Detected labels: {labels}')
    except Exception as e:
        print(f'[AWS REKOGNITION WARNING] Scan failed (maybe not an image?): {e}')

    return s3_link


def ensure_rating_record(user_id):
    """Create the DynamoDB rating row for a new user (partition key user_id)."""
    table = _ratings_table()
    try:
        table.put_item(
            Item={'user_id': int(user_id), 'avg_rating': Decimal('0'), 'review_count': 0},
            ConditionExpression='attribute_not_exists(user_id)',
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def update_rating_metrics(user_id, avg_rating, review_count):
    """Updates rating metrics in Amazon DynamoDB (required)."""
    table = _ratings_table()
    print(f'\n[AWS DYNAMODB] Updating Rating Metrics for User ID {user_id}...')
    table.update_item(
        Key={'user_id': int(user_id)},
        UpdateExpression='SET avg_rating=:a, review_count=:r',
        ExpressionAttributeValues={
            ':a': Decimal(str(avg_rating)),
            ':r': int(review_count),
        },
    )
    print(f' -> Result: New Average Rating: {avg_rating:.1f} | Total Reviews: {review_count}\n')
    return True


def generate_ai_summary(session_id, transcript='Session transcript simulation.', subject='the session'):
    """
    Generates a summary using Amazon Bedrock.
    """
    print(f'\n[AWS BEDROCK] Generating summary for session {session_id}...')
    bedrock = boto3.client('bedrock-runtime', region_name=get_region())

    prompt = (
        f"Please summarize the following meeting transcript about '{subject}' "
        f'into 3 key takeaways:\n\n{transcript}\n\nSummary:'
    )

    body = json.dumps(
        {
            'inputText': prompt,
            'textGenerationConfig': {
                'maxTokenCount': 200,
                'temperature': 0.7,
            },
        }
    )

    response = bedrock.invoke_model(
        body=body,
        modelId='amazon.titan-text-lite-v1',
        accept='application/json',
        contentType='application/json',
    )

    response_body = json.loads(response.get('body').read())
    summary = response_body.get('results')[0].get('outputText').strip()
    print('[AWS BEDROCK] Summary generated successfully.')
    return summary


def publish_notification(message, subject='BridgeTheGap Notification'):
    """Publishes to Amazon SNS (required)."""
    topic_arn = _sns_topic_arn()
    sns = boto3.client('sns', region_name=get_region())
    print(f'\n[AWS SNS] Publishing notification to {topic_arn}...')
    sns.publish(
        TopicArn=topic_arn,
        Message=message,
        Subject=subject[:100] if subject else 'BridgeTheGap',
    )
    print('[AWS SNS] Notification published successfully.')
    return True
