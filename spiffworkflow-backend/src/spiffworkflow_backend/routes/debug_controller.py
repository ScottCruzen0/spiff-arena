import json

import redis
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from flask import current_app
from flask import jsonify
from flask import make_response
from flask import request
from flask.wrappers import Response

from spiffworkflow_backend.exceptions.api_error import ApiError
from spiffworkflow_backend.services.authentication_service import AuthenticationService
from spiffworkflow_backend.services.monitoring_service import get_version_info_data


def test_raise_error() -> Response:
    raise Exception("This exception was generated by /debug/test-raise-error for testing purposes. Please ignore.")


def version_info() -> Response:
    return make_response(get_version_info_data(), 200)


# this is just to see what the protocol is, primarily. if the site is running on https in the browser, but this says "http://something.example.com",
# that might be bad, and might require some server configuration to make sure flask knows it is running on https.
# if using path based routing, the path will probably not be returned from this endpoint.
def url_info() -> Response:
    return make_response(
        {
            "request.root_path": request.root_path,  # type: ignore
            "request.host_url": request.host_url,
            "request.url": request.url,
            "cache": AuthenticationService.ENDPOINT_CACHE,
        },
        200,
    )


def get_redis_results(redis_client):
    results = redis_client.keys("celery-task-meta-*")
    if len(results) > 1000:
        raise ApiError(
            error_code="too_many_entries",
            message=f"There are too many redis entries. You probably shouldn't use this api method. count {len(results)}",
            status_code=400,
        )

    return redis_client.mget(results)


def get_s3_results(s3_client, bucket_name):
    try:
        # This defaults to retrieving a maximum of 1000 items, if it
        # does return 1000 items, maybe it should also raise an exception
        # the way the redis backend code does.
        objects = s3_client.list_objects_v2(
            Bucket=bucket_name, Prefix="celery-task-meta-"
        )
        if "Contents" not in objects:
            return []

        result_values = []
        for obj in objects["Contents"]:
            response = s3_client.get_object(Bucket=bucket_name, Key=obj["Key"])
            result_values.append(response["Body"].read())

        return result_values
    except (BotoCoreError, ClientError) as e:
        raise ApiError(
            error_code="s3_error",
            message=str(e),
            status_code=500,
        ) from e


def celery_backend_results(
    process_instance_id: int,
    include_all_failures: bool = True,
) -> Response:
    backend_url = current_app.config["SPIFFWORKFLOW_BACKEND_CELERY_RESULT_BACKEND"]

    if backend_url:
        if backend_url.startswith("redis://"):
            redis_client = redis.StrictRedis.from_url(backend_url)
            result_values = get_redis_results(redis_client)
        elif backend_url.startswith("s3://"):
            s3_client = boto3.client("s3")
            bucket_name = current_app.config[
                "SPIFFWORKFLOW_BACKEND_CELERY_RESULT_S3_BUCKET"
            ]
            result_values = get_s3_results(s3_client, bucket_name)
        else:
            raise ApiError(
                error_code="unsupported_backend",
                message="The specified backend is not supported.",
                status_code=500,
            )
    else:
        raise ApiError(
            error_code="no_results_backend",
            message="No Celery results backend configured.",
            status_code=500,
        )

    return_body = []
    for value in result_values:
        if value is None:
            continue
        value_dict = json.loads(value.decode("utf-8"))
        if (
            value_dict["result"]
            and "process_instance_id" in value_dict["result"]
            and value_dict["result"]["process_instance_id"] == process_instance_id
        ) or (value_dict["status"] == "FAILURE" and include_all_failures is True):
            return_body.append(value_dict)

    return make_response(jsonify(return_body), 200)
