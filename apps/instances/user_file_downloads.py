from django.http import FileResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header
from google.api_core.exceptions import NotFound
from storages.backends.gcloud import GoogleCloudStorage
from storages.utils import clean_name


GCS_FETCH_BYTES = 4 * 1024 * 1024
RESPONSE_CHUNK_BYTES = 64 * 1024
GCS_REQUEST_TIMEOUT_SECONDS = 30


def _gcs_chunks(blob, size):
    # 객체 세대는 응답을 만들기 전에 고정한다
    with blob.open(
        "rb", chunk_size=GCS_FETCH_BYTES, raw_download=True,
        timeout=GCS_REQUEST_TIMEOUT_SECONDS,
    ) as reader:
        remaining = size
        while remaining:
            chunk = reader.read(min(RESPONSE_CHUNK_BYTES, remaining))
            if not chunk or len(chunk) > remaining:
                raise OSError("Challenge file stream length does not match its metadata")
            remaining -= len(chunk)
            yield chunk


def user_file_response(bundle, storage):
    if not isinstance(storage, GoogleCloudStorage):
        if not storage.exists(bundle.object_key):
            raise FileNotFoundError
        return FileResponse(
            storage.open(bundle.object_key, "rb"),
            as_attachment=True,
            filename="user-files.zip",
            content_type="application/zip",
        )

    # GoogleCloudFile.read는 전체 파일을 먼저 메모리에 적재하므로 BlobReader를 사용한다
    name = storage._normalize_name(clean_name(bundle.object_key))
    blob = storage.bucket.blob(name)
    try:
        blob.reload(timeout=GCS_REQUEST_TIMEOUT_SECONDS)
    except NotFound:
        raise FileNotFoundError from None
    if blob.generation is None or blob.size != bundle.size_bytes or bundle.size_bytes < 0:
        raise FileNotFoundError

    pinned_blob = storage.bucket.blob(name, generation=blob.generation)
    response = StreamingHttpResponse(
        _gcs_chunks(pinned_blob, bundle.size_bytes), content_type="application/zip"
    )
    response["Content-Length"] = str(bundle.size_bytes)
    response["Content-Disposition"] = content_disposition_header(True, "user-files.zip")
    return response
