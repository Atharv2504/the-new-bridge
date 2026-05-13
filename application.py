"""WSGI entry for AWS Elastic Beanstalk, App Runner, or EC2 + gunicorn."""

from api.index import app as application
