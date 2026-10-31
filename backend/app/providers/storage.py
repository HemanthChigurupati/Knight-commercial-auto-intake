"""Blob storage for raw submissions and derived artifacts (rendered pages, extracted images).

  LocalStorage -- a folder on disk                    [demo / local]
  S3Storage    -- an S3 bucket, SSE-KMS encrypted       [aws]

The pipeline always works on a local working copy; S3 is the durable system of record.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import Settings


class Storage(Protocol):
    def put(self, key: str, local_path: Path) -> str: ...
    def get(self, key: str, local_path: Path) -> Path: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, key, local_path):
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.resolve() != Path(local_path).resolve():
            dest.write_bytes(Path(local_path).read_bytes())
        return f"file://{dest}"

    def get(self, key, local_path):
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes((self.root / key).read_bytes())
        return local_path


class S3Storage:
    def __init__(self, settings: Settings):
        import boto3
        session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
        self.s3 = session.client("s3")
        self.bucket = settings.s3_bucket

    def put(self, key, local_path):
        self.s3.upload_file(str(local_path), self.bucket, key,
                            ExtraArgs={"ServerSideEncryption": "aws:kms"})
        return f"s3://{self.bucket}/{key}"

    def get(self, key, local_path):
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.bucket, key, str(local_path))
        return Path(local_path)


def build_storage(settings: Settings) -> Storage:
    if settings.mode == "aws":
        return S3Storage(settings)
    return LocalStorage(settings.data_dir / "blobs")
