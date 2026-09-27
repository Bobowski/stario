# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, freethreading_compatible=True
"""Python face of the native brotli / zstd codecs the HTTP writer already uses."""

from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_FromStringAndSize, PyBytes_GET_SIZE

from stario.exceptions import StarioError

from stario_cython.compression_buf cimport (
    StarioBrotli,
    StarioBrotliDecoder,
    StarioZstd,
    StarioZstdDecoder,
    stario_brotli_acquire,
    stario_brotli_block_borrowed,
    stario_brotli_decoder_finished,
    stario_brotli_decoder_free,
    stario_brotli_decoder_new,
    stario_brotli_decoder_push_borrowed,
    stario_brotli_decompress_borrowed,
    stario_brotli_finish_borrowed,
    stario_brotli_release,
    stario_zstd_acquire,
    stario_zstd_block_borrowed,
    stario_zstd_decoder_finished,
    stario_zstd_decoder_free,
    stario_zstd_decoder_new,
    stario_zstd_decoder_push_borrowed,
    stario_zstd_decompress_borrowed,
    stario_zstd_finish_borrowed,
    stario_zstd_release,
)


cdef object _owned_bytes(const unsigned char* data, size_t n):
    if n == 0:
        return b""
    return PyBytes_FromStringAndSize(<const char*>data, <Py_ssize_t>n)


cdef inline const unsigned char* _bytes_ptr(bytes data, size_t* n) noexcept:
    n[0] = <size_t>PyBytes_GET_SIZE(data)
    if n[0] == 0:
        return NULL
    return <const unsigned char*>PyBytes_AS_STRING(data)


def brotli_compress(bytes data, int level=4, int window=0) -> bytes:
    cdef StarioBrotli* enc = stario_brotli_acquire(level, window)
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    if enc == NULL:
        raise StarioError("brotli stream init failed")
    try:
        ptr = _bytes_ptr(data, &n)
        if stario_brotli_finish_borrowed(enc, ptr, n, &out, &out_len) != 0:
            raise StarioError("brotli compression failed")
        return _owned_bytes(out, out_len)
    finally:
        stario_brotli_release(enc)


def brotli_decompress(bytes data) -> bytes:
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    ptr = _bytes_ptr(data, &n)
    if stario_brotli_decompress_borrowed(ptr, n, &out, &out_len) != 0:
        raise StarioError("brotli decompression failed")
    return _owned_bytes(out, out_len)


def zstd_compress(bytes data, int level=3, int window=0) -> bytes:
    cdef StarioZstd* enc = stario_zstd_acquire(level, window)
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    if enc == NULL:
        raise StarioError("zstd stream init failed")
    try:
        ptr = _bytes_ptr(data, &n)
        if stario_zstd_finish_borrowed(enc, ptr, n, &out, &out_len) != 0:
            raise StarioError("zstd compression failed")
        return _owned_bytes(out, out_len)
    finally:
        stario_zstd_release(enc)


def zstd_decompress(bytes data) -> bytes:
    cdef const unsigned char* out = NULL
    cdef size_t out_len = 0
    cdef const unsigned char* ptr
    cdef size_t n
    ptr = _bytes_ptr(data, &n)
    if stario_zstd_decompress_borrowed(ptr, n, &out, &out_len) != 0:
        raise StarioError("zstd decompression failed")
    return _owned_bytes(out, out_len)


cdef class BrotliEncoder:
    cdef StarioBrotli* _enc

    def __cinit__(self, int level, int window=0):
        self._enc = stario_brotli_acquire(level, window)
        if self._enc == NULL:
            raise StarioError("brotli stream init failed")

    def __dealloc__(self):
        if self._enc != NULL:
            stario_brotli_release(self._enc)
            self._enc = NULL

    def block(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        if self._enc == NULL:
            raise StarioError("brotli encoder is finished")
        ptr = _bytes_ptr(data, &n)
        if stario_brotli_block_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
            raise StarioError("brotli stream failed")
        return _owned_bytes(out, out_len)

    def finish(self, bytes data=b"") -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        if self._enc == NULL:
            return b""
        try:
            ptr = _bytes_ptr(data, &n)
            if stario_brotli_finish_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
                raise StarioError("brotli finish failed")
            return _owned_bytes(out, out_len)
        finally:
            stario_brotli_release(self._enc)
            self._enc = NULL


cdef class ZstdEncoder:
    cdef StarioZstd* _enc

    def __cinit__(self, int level, int window=0):
        self._enc = stario_zstd_acquire(level, window)
        if self._enc == NULL:
            raise StarioError("zstd stream init failed")

    def __dealloc__(self):
        if self._enc != NULL:
            stario_zstd_release(self._enc)
            self._enc = NULL

    def block(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        if self._enc == NULL:
            raise StarioError("zstd encoder is finished")
        ptr = _bytes_ptr(data, &n)
        if stario_zstd_block_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
            raise StarioError("zstd stream failed")
        return _owned_bytes(out, out_len)

    def finish(self, bytes data=b"") -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        if self._enc == NULL:
            return b""
        try:
            ptr = _bytes_ptr(data, &n)
            if stario_zstd_finish_borrowed(self._enc, ptr, n, &out, &out_len) != 0:
                raise StarioError("zstd finish failed")
            return _owned_bytes(out, out_len)
        finally:
            stario_zstd_release(self._enc)
            self._enc = NULL


cdef class BrotliDecoder:
    cdef StarioBrotliDecoder* _dec

    def __cinit__(self):
        self._dec = stario_brotli_decoder_new()
        if self._dec == NULL:
            raise StarioError("brotli decoder init failed")

    def __dealloc__(self):
        if self._dec != NULL:
            stario_brotli_decoder_free(self._dec)
            self._dec = NULL

    def decompress(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        ptr = _bytes_ptr(data, &n)
        if stario_brotli_decoder_push_borrowed(self._dec, ptr, n, &out, &out_len) != 0:
            raise StarioError("brotli decompression failed")
        return _owned_bytes(out, out_len)

    def is_finished(self) -> bool:
        return stario_brotli_decoder_finished(self._dec) != 0


cdef class ZstdDecoder:
    cdef StarioZstdDecoder* _dec

    def __cinit__(self):
        self._dec = stario_zstd_decoder_new()
        if self._dec == NULL:
            raise StarioError("zstd decoder init failed")

    def __dealloc__(self):
        if self._dec != NULL:
            stario_zstd_decoder_free(self._dec)
            self._dec = NULL

    def decompress(self, bytes data) -> bytes:
        cdef const unsigned char* out = NULL
        cdef size_t out_len = 0
        cdef const unsigned char* ptr
        cdef size_t n
        ptr = _bytes_ptr(data, &n)
        if stario_zstd_decoder_push_borrowed(self._dec, ptr, n, &out, &out_len) != 0:
            raise StarioError("zstd decompression failed")
        return _owned_bytes(out, out_len)

    def is_finished(self) -> bool:
        return stario_zstd_decoder_finished(self._dec) != 0
