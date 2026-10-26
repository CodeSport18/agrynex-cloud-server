import boto3
from botocore.config import Config as BotoConfig


class MediaStorage:
    """Alert snapshots and clips in a private S3 bucket.

    The Pi uploads straight to S3 with pre-signed PUT links, and the app and
    dashboard view media through pre-signed GET links, so the bucket never
    needs to be public.
    """

    def __init__(self, bucket, region, endpoint_url=None, url_ttl_s=3600):
        self.bucket = bucket
        self.url_ttl_s = url_ttl_s
        self.s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            config=BotoConfig(signature_version="s3v4"),
        )

    def upload_url(self, key, content_type):
        return self.s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=self.url_ttl_s,
        )

    def view_url(self, key):
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.url_ttl_s,
        )

    def exists(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except self.s3.exceptions.ClientError:
            return False

    def delete(self, keys):
        keys = [k for k in keys if k]
        # S3 accepts up to 1000 keys per request.
        for i in range(0, len(keys), 1000):
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True},
            )
