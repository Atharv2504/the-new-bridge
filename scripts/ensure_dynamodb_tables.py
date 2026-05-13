#!/usr/bin/env python3
"""
Create BridgeTheGap DynamoDB tables if they do not exist.

  Ratings:   partition key user_id (Number) — avg_rating / review_count set by the app
  Sessions:  partition key session_id (String) — payload + ttl set by the app; TTL enabled on ttl

Usage (from repo root, with AWS credentials configured):

  python scripts/ensure_dynamodb_tables.py --ratings-table MyRatings --sessions-table MySessions

Or set non-empty AWS_DYNAMODB_TABLE_NAME and AWS_DYNAMODB_SESSION_TABLE_NAME in .env.production.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Repo root = parent of scripts/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    for name in ('.env.production', '.env'):
        p = ROOT / name
        if p.is_file():
            load_dotenv(p)
            break
except ImportError:
    pass

import boto3
from botocore.exceptions import ClientError


def region() -> str:
    return (
        os.environ.get('AWS_DEFAULT_REGION')
        or os.environ.get('AWS_REGION')
        or 'us-east-1'
    )


def ensure_ratings_table(client, name: str) -> None:
    try:
        client.create_table(
            TableName=name,
            BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'user_id', 'AttributeType': 'N'}],
            KeySchema=[{'AttributeName': 'user_id', 'KeyType': 'HASH'}],
        )
        print(f'Created ratings table: {name}')
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f'Ratings table already exists: {name}')
        else:
            raise
    waiter = client.get_waiter('table_exists')
    waiter.wait(TableName=name)


def ensure_session_table(client, name: str) -> None:
    try:
        client.create_table(
            TableName=name,
            BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': 'session_id', 'AttributeType': 'S'}],
            KeySchema=[{'AttributeName': 'session_id', 'KeyType': 'HASH'}],
        )
        print(f'Created session table: {name}')
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f'Session table already exists: {name}')
        else:
            raise
    waiter = client.get_waiter('table_exists')
    waiter.wait(TableName=name)

    # Enable TTL on attribute "ttl" (epoch seconds). Safe to call repeatedly.
    try:
        client.update_time_to_live(
            TableName=name,
            TimeToLiveSpecification={'Enabled': True, 'AttributeName': 'ttl'},
        )
        print(f'Enabled TTL on {name} (attribute ttl)')
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('ValidationException', 'ResourceInUseException'):
            print(f'TTL on {name}: {e.response["Error"].get("Message", code)}')
        else:
            raise


def main() -> int:
    p = argparse.ArgumentParser(description='Create BridgeTheGap DynamoDB tables')
    p.add_argument(
        '--ratings-table',
        default=(os.environ.get('AWS_DYNAMODB_TABLE_NAME') or '').strip() or None,
        help='Ratings table name (default: env AWS_DYNAMODB_TABLE_NAME)',
    )
    p.add_argument(
        '--sessions-table',
        default=(os.environ.get('AWS_DYNAMODB_SESSION_TABLE_NAME') or '').strip() or None,
        help='Flask session table name (default: env AWS_DYNAMODB_SESSION_TABLE_NAME)',
    )
    args = p.parse_args()
    ratings = args.ratings_table
    sessions = args.sessions_table
    if not ratings or not sessions:
        print(
            'Provide table names: --ratings-table NAME --sessions-table NAME\n'
            'Or set non-empty AWS_DYNAMODB_TABLE_NAME and AWS_DYNAMODB_SESSION_TABLE_NAME.',
            file=sys.stderr,
        )
        return 1

    client = boto3.client('dynamodb', region_name=region())
    ensure_ratings_table(client, ratings)
    ensure_session_table(client, sessions)
    print('Done.')
    print(f'Export for the app: AWS_DYNAMODB_TABLE_NAME={ratings}')
    print(f'Export for the app: AWS_DYNAMODB_SESSION_TABLE_NAME={sessions}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
