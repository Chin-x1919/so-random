import os
import base64
import logging
import threading
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

# ---------------- Configuration ----------------

RNG_DEVICE = os.getenv("RNG_DEVICE", "/dev/hwrng")
MAX_BYTES = int(os.getenv("RNG_MAX_BYTES", "4096"))

DEFAULT_ORIGINS = (
    "http://localhost:8000,"
    "http://127.0.0.1:8000,"
    "http://localhost:8080,"
    "http://127.0.0.1:8080"
)

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "RNG_CORS_ORIGINS", DEFAULT_ORIGINS
    ).split(",")
    if origin.strip()
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("pi-rng")

# Serialize access to the hardware device.
rng_lock = threading.Lock()

app = FastAPI(
    title="Pi Hardware RNG API",
    description="Random bytes served by a Raspberry Pi hardware RNG.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------- RNG access ----------------

class RNGUnavailable(Exception):
    pass


@contextmanager
def open_rng():
    """Open the device for one operation and always close it."""
    try:
        fd = os.open(RNG_DEVICE, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise RNGUnavailable(
            f"Cannot open {RNG_DEVICE}: {exc}"
        ) from exc

    try:
        yield fd
    finally:
        os.close(fd)


def read_hwrng(count: int) -> bytes:
    """Read exactly count bytes or fail; never return a short result."""
    if count < 1 or count > MAX_BYTES:
        raise ValueError("Requested byte count is outside allowed limits")

    result = bytearray()

    try:
        with rng_lock:
            with open_rng() as fd:
                while len(result) < count:
                    chunk = os.read(fd, count - len(result))

                    if not chunk:
                        raise RNGUnavailable(
                            "Hardware RNG returned no data"
                        )

                    result.extend(chunk)

    except RNGUnavailable:
        raise
    except OSError as exc:
        raise RNGUnavailable(
            f"Hardware RNG read failed: {exc}"
        ) from exc

    return bytes(result)


def rng_error(exc: Exception) -> HTTPException:
    log.error("RNG operation failed: %s", exc)
    return HTTPException(
        status_code=503,
        detail="Hardware RNG is currently unavailable",
    )


# ---------------- Endpoints ----------------

@app.get("/")
def index():
    return {
        "service": "Pi Hardware RNG API",
        "version": app.version,
        "endpoints": {
            "hex": "/hex?n=32",
            "base64": "/base64?n=32",
            "raw": "/raw?n=32",
            "int": "/int?min=0&max=100",
            "health": "/health",
            "rng_health": "/health/rng",
        },
        "max_bytes_per_request": MAX_BYTES,
    }


@app.get("/health")
def health():
    # Liveness/configuration check only.
    # It does not prove the RNG is producing valid entropy.
    return {
        "status": "ok",
        "device": RNG_DEVICE,
        "device_exists": os.path.exists(RNG_DEVICE),
        "device_readable_by_process": os.access(
            RNG_DEVICE, os.R_OK
        ),
    }


@app.get("/health/rng")
def rng_health():
    # Actually attempt a one-byte read.
    try:
        sample = read_hwrng(1)
    except (RNGUnavailable, ValueError) as exc:
        raise rng_error(exc) from exc

    return {
        "status": "read_ok",
        "bytes_read": len(sample),
        "note": "Read test only; not an entropy quality test.",
    }


@app.get("/hex")
def get_hex(
    n: int = Query(default=32, ge=1, le=MAX_BYTES)
):
    try:
        data = read_hwrng(n)
    except (RNGUnavailable, ValueError) as exc:
        raise rng_error(exc) from exc

    return {
        "bytes": len(data),
        "hex": data.hex(),
    }


@app.get("/base64")
def get_base64(
    n: int = Query(default=32, ge=1, le=MAX_BYTES)
):
    try:
        data = read_hwrng(n)
    except (RNGUnavailable, ValueError) as exc:
        raise rng_error(exc) from exc

    return {
        "bytes": len(data),
        "base64": base64.b64encode(data).decode("ascii"),
    }


@app.get("/raw")
def get_raw(
    n: int = Query(default=32, ge=1, le=MAX_BYTES)
):
    try:
        data = read_hwrng(n)
    except (RNGUnavailable, ValueError) as exc:
        raise rng_error(exc) from exc

    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/int")
def get_int(
    min_value: int = Query(default=0, alias="min"),
    max_value: int = Query(default=100, alias="max"),
):
    if min_value > max_value:
        raise HTTPException(
            status_code=400,
            detail="'min' must be less than or equal to 'max'",
        )

    span = max_value - min_value + 1

    # Keep requests bounded and arithmetic manageable.
    if span > 2**32:
        raise HTTPException(
            status_code=400,
            detail="Integer range cannot exceed 2^32 possible values",
        )

    if span == 1:
        return {"min": min_value, "max": max_value, "value": min_value}

    # Rejection sampling avoids modulo bias, assuming the input
    # bytes are uniformly distributed.
    byte_count = max(1, ((span - 1).bit_length() + 7) // 8)
    space = 1 << (8 * byte_count)
    limit = space - (space % span)

    try:
        for _ in range(16):
            data = read_hwrng(byte_count)
            value = int.from_bytes(data, "big")

            if value < limit:
                return {
                    "min": min_value,
                    "max": max_value,
                    "value": min_value + (value % span),
                }

    except (RNGUnavailable, ValueError) as exc:
        raise rng_error(exc) from exc

    raise HTTPException(
        status_code=503,
        detail="Unable to generate an accepted integer; retry later",
    )
