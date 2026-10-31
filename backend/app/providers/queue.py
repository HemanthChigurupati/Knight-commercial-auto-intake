"""Job queue between intake and the processing worker.

  InProcessQueue -- runs the job immediately in the same process   [demo / local]
  SQSQueue       -- Amazon SQS; a worker polls and processes        [aws]

Decoupling intake from processing is what lets the system absorb bursts of email:
intake only stores files and enqueues a message; workers scale on queue depth.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from ..config import Settings


class JobQueue(Protocol):
    def submit(self, job: dict) -> None: ...


class InProcessQueue:
    def __init__(self, handler: Callable[[dict], None]):
        self.handler = handler

    def submit(self, job):
        self.handler(job)


class SQSQueue:
    def __init__(self, settings: Settings):
        import boto3
        session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        self.sqs = session.client("sqs")
        self.url = settings.sqs_queue_url

    def submit(self, job):
        self.sqs.send_message(QueueUrl=self.url, MessageBody=json.dumps(job))

    def poll(self, handler: Callable[[dict], None], wait_seconds: int = 20) -> None:
        """Worker loop body: receive, process, delete. Failed jobs reappear after the
        visibility timeout and land in a dead-letter queue after max receives."""
        resp = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1,
                                        WaitTimeSeconds=wait_seconds)
        for msg in resp.get("Messages", []):
            handler(json.loads(msg["Body"]))
            self.sqs.delete_message(QueueUrl=self.url, ReceiptHandle=msg["ReceiptHandle"])


def build_queue(settings: Settings, handler: Callable[[dict], None]) -> JobQueue:
    if settings.mode == "aws" and settings.sqs_queue_url:
        return SQSQueue(settings)
    return InProcessQueue(handler)
