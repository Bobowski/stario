#include "stario_h1.h"

#include <string.h>

#ifdef __SSE2__
#include <emmintrin.h>
#endif

#ifndef STARIO_H1_MAX_HEADERS
#define STARIO_H1_MAX_HEADERS 64
#endif

typedef struct {
  const char* name;
  size_t name_len;
  const char* value;
  size_t value_len;
} stario_h1_hdr;

enum {
  H1_OK = 0,
  H1_ERR = 1,
  H1_INC = 2
};

/* RFC 9110 tchar */
static const unsigned char H1_TCHAR[256] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1, 0, 0, 1, 1, 0, 1, 1, 0,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};

/* HTAB, SP..tilde except DEL, plus obs-text. */
static const unsigned char H1_VALUE[256] = {
    0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1};

static const char* find_crlfcrlf(const char* p, const char* end) {
#ifdef __SSE2__
  const __m128i cr = _mm_set1_epi8('\r');
  while (p + 16 <= end) {
    __m128i chunk = _mm_loadu_si128((const __m128i*)(const void*)p);
    int mask = _mm_movemask_epi8(_mm_cmpeq_epi8(chunk, cr));
    while (mask) {
      int i = __builtin_ctz(mask);
      if (p + i + 3 < end && p[i + 1] == '\n' && p[i + 2] == '\r' &&
          p[i + 3] == '\n') {
        return p + i;
      }
      mask &= mask - 1;
    }
    p += 16;
  }
#else
  while (p + 8 <= end) {
    uint64_t word;
    memcpy(&word, p, 8);
    uint64_t x = word ^ 0x0d0d0d0d0d0d0d0dULL;
    uint64_t has = (x - 0x0101010101010101ULL) & ~x & 0x8080808080808080ULL;
    if (has) {
      size_t i;
      for (i = 0; i < 8; i++) {
        if (p[i] == '\r' && p + i + 3 < end && p[i + 1] == '\n' &&
            p[i + 2] == '\r' && p[i + 3] == '\n') {
          return p + i;
        }
      }
    }
    p += 7;
  }
#endif
  while (p + 3 < end) {
    if (p[0] == '\r' && p[1] == '\n' && p[2] == '\r' && p[3] == '\n') {
      return p;
    }
    p++;
  }
  return NULL;
}

static int parse_method(const char* p, size_t n, uint8_t* out) {
  switch (n) {
    case 3:
      if (p[0] == 'G' && p[1] == 'E' && p[2] == 'T') {
        *out = HTTP_GET;
        return H1_OK;
      }
      if (p[0] == 'P' && p[1] == 'U' && p[2] == 'T') {
        *out = HTTP_PUT;
        return H1_OK;
      }
      return H1_INC;
    case 4:
      if (p[0] == 'P' && p[1] == 'O' && p[2] == 'S' && p[3] == 'T') {
        *out = HTTP_POST;
        return H1_OK;
      }
      if (p[0] == 'H' && p[1] == 'E' && p[2] == 'A' && p[3] == 'D') {
        *out = HTTP_HEAD;
        return H1_OK;
      }
      return H1_INC;
    case 5:
      if (memcmp(p, "PATCH", 5) == 0) {
        *out = HTTP_PATCH;
        return H1_OK;
      }
      if (memcmp(p, "TRACE", 5) == 0) {
        *out = HTTP_TRACE;
        return H1_OK;
      }
      if (memcmp(p, "QUERY", 5) == 0) {
        *out = HTTP_QUERY;
        return H1_OK;
      }
      return H1_INC;
    case 6:
      if (memcmp(p, "DELETE", 6) == 0) {
        *out = HTTP_DELETE;
        return H1_OK;
      }
      return H1_INC;
    case 7:
      if (memcmp(p, "OPTIONS", 7) == 0) {
        *out = HTTP_OPTIONS;
        return H1_OK;
      }
      if (memcmp(p, "CONNECT", 7) == 0) {
        *out = HTTP_CONNECT;
        return H1_OK;
      }
      return H1_INC;
    default:
      return H1_INC;
  }
}

static int name_eq(const char* p, size_t n, const char* lit, size_t lit_n) {
  size_t i;
  unsigned char c;
  if (n != lit_n) {
    return 0;
  }
  for (i = 0; i < n; i++) {
    c = (unsigned char)p[i];
    if (c >= 'A' && c <= 'Z') {
      c = (unsigned char)(c + 32);
    }
    if (c != (unsigned char)lit[i]) {
      return 0;
    }
  }
  return 1;
}

static int valid_url(const char* p, size_t n) {
  size_t i;
  unsigned char c;
  if (n == 0) {
    return 0;
  }
  for (i = 0; i < n; i++) {
    c = (unsigned char)p[i];
    if (c <= 0x20 || c == 0x7f) {
      return 0;
    }
  }
  return 1;
}

static int valid_value(const char* p, size_t n) {
  size_t i;
  for (i = 0; i < n; i++) {
    if (!H1_VALUE[(unsigned char)p[i]]) {
      return 0;
    }
  }
  return 1;
}

static int parse_content_length(const char* p, size_t n, uint64_t* out) {
  uint64_t value = 0;
  size_t i;
  if (n == 0) {
    return H1_ERR;
  }
  for (i = 0; i < n; i++) {
    unsigned char c = (unsigned char)p[i];
    uint64_t next;
    if (c < '0' || c > '9') {
      return H1_ERR;
    }
    next = value * 10u + (uint64_t)(c - '0');
    if (next < value) {
      return H1_ERR;
    }
    value = next;
  }
  *out = value;
  return H1_OK;
}

static void scan_connection(const char* p, size_t n, uint16_t* flags) {
  size_t i = 0;
  while (i < n) {
    size_t start;
    size_t end;
    while (i < n && (p[i] == ' ' || p[i] == '\t' || p[i] == ',')) {
      i++;
    }
    start = i;
    while (i < n && p[i] != ',') {
      i++;
    }
    end = i;
    while (end > start && (p[end - 1] == ' ' || p[end - 1] == '\t')) {
      end--;
    }
    if (end > start) {
      size_t tok = end - start;
      if (name_eq(p + start, tok, "close", 5)) {
        *flags |= F_CONNECTION_CLOSE;
      } else if (name_eq(p + start, tok, "keep-alive", 10)) {
        *flags |= F_CONNECTION_KEEP_ALIVE;
      } else if (name_eq(p + start, tok, "upgrade", 7)) {
        *flags |= F_CONNECTION_UPGRADE;
      }
    }
    if (i < n && p[i] == ',') {
      i++;
    }
  }
}

static int te_is_chunked(const char* p, size_t n) {
  size_t i = 0;
  int saw_chunked = 0;
  while (i < n) {
    size_t start;
    size_t end;
    size_t semi;
    while (i < n && (p[i] == ' ' || p[i] == '\t' || p[i] == ',')) {
      i++;
    }
    start = i;
    while (i < n && p[i] != ',') {
      i++;
    }
    end = i;
    while (end > start && (p[end - 1] == ' ' || p[end - 1] == '\t')) {
      end--;
    }
    semi = start;
    while (semi < end && p[semi] != ';') {
      semi++;
    }
    while (semi > start && (p[semi - 1] == ' ' || p[semi - 1] == '\t')) {
      semi--;
    }
    if (semi > start) {
      if (name_eq(p + start, semi - start, "chunked", 7)) {
        saw_chunked = 1;
      } else {
        return 0;
      }
    }
    if (i < n && p[i] == ',') {
      i++;
    }
  }
  return saw_chunked;
}

static int is_expect_continue(const char* p, size_t n) {
  size_t i = 0;
  while (i < n) {
    size_t start;
    size_t end;
    while (i < n && (p[i] == ' ' || p[i] == '\t' || p[i] == ',')) {
      i++;
    }
    start = i;
    while (i < n && p[i] != ',') {
      i++;
    }
    end = i;
    while (end > start && (p[end - 1] == ' ' || p[end - 1] == '\t')) {
      end--;
    }
    if (end > start && name_eq(p + start, end - start, "100-continue", 12)) {
      return 1;
    }
    if (i < n && p[i] == ',') {
      i++;
    }
  }
  return 0;
}

static int fire_cb(llhttp_t* parser, llhttp_cb fn) {
  if (fn == NULL) {
    return 0;
  }
  return fn(parser);
}

static int fire_data(
    llhttp_t* parser,
    llhttp_data_cb fn,
    const char* at,
    size_t n
) {
  if (fn == NULL || n == 0) {
    return 0;
  }
  return fn(parser, at, n);
}

int64_t stario_h1_try(
    llhttp_t* parser,
    const llhttp_settings_t* settings,
    const char* data,
    size_t len,
    size_t max_message
) {
  const char* end;
  const char* headers_term;
  const char* line_end;
  const char* p;
  const char* method;
  const char* url;
  const char* version;
  size_t method_len;
  size_t url_len;
  stario_h1_hdr headers[STARIO_H1_MAX_HEADERS];
  size_t header_count = 0;
  uint8_t http_method = 0;
  uint8_t minor = 1;
  uint16_t flags = 0;
  uint64_t content_length = 0;
  int has_cl = 0;
  int has_te = 0;
  int expect_continue = 0;
  size_t header_bytes;
  size_t total;
  size_t i;
  int rc;

  if (parser == NULL || settings == NULL || data == NULL) {
    return STARIO_H1_ERROR;
  }
  if (len < 16) {
    return STARIO_H1_INCOMPLETE;
  }
  end = data + len;
  headers_term = find_crlfcrlf(data, end);
  if (headers_term == NULL) {
    return STARIO_H1_INCOMPLETE;
  }

  /* Request line: METHOD SP target SP HTTP/1.x CRLF */
  line_end = memchr(data, '\n', (size_t)(headers_term + 3 - data));
  if (line_end == NULL || line_end == data || line_end[-1] != '\r') {
    return STARIO_H1_ERROR;
  }
  line_end--; /* points at CR */

  p = memchr(data, ' ', (size_t)(line_end - data));
  if (p == NULL || p == data) {
    return STARIO_H1_ERROR;
  }
  method = data;
  method_len = (size_t)(p - data);
  p++;
  url = p;
  p = memchr(url, ' ', (size_t)(line_end - url));
  if (p == NULL || p == url) {
    return STARIO_H1_ERROR;
  }
  url_len = (size_t)(p - url);
  version = p + 1;
  if ((size_t)(line_end - version) != 8 || memcmp(version, "HTTP/1.", 7) != 0) {
    return STARIO_H1_INCOMPLETE;
  }
  if (version[7] == '1') {
    minor = 1;
  } else if (version[7] == '0') {
    minor = 0;
  } else {
    return STARIO_H1_INCOMPLETE;
  }
  rc = parse_method(method, method_len, &http_method);
  if (rc != H1_OK) {
    return STARIO_H1_INCOMPLETE;
  }
  if (!valid_url(url, url_len)) {
    return STARIO_H1_ERROR;
  }

  p = line_end + 2;
  while (p < headers_term) {
    const char* colon;
    const char* value;
    size_t name_len;
    size_t value_len;
    const char* row_end = memchr(p, '\n', (size_t)(headers_term + 3 - p));
    if (row_end == NULL || row_end == p || row_end[-1] != '\r') {
      return STARIO_H1_ERROR;
    }
    row_end--;
    if (p[0] == ' ' || p[0] == '\t') {
      return STARIO_H1_ERROR;
    }
    colon = memchr(p, ':', (size_t)(row_end - p));
    if (colon == NULL || colon == p) {
      return STARIO_H1_ERROR;
    }
    name_len = (size_t)(colon - p);
    for (i = 0; i < name_len; i++) {
      if (!H1_TCHAR[(unsigned char)p[i]]) {
        return STARIO_H1_ERROR;
      }
    }
    value = colon + 1;
    while (value < row_end && (*value == ' ' || *value == '\t')) {
      value++;
    }
    value_len = (size_t)(row_end - value);
    while (value_len > 0 &&
           (value[value_len - 1] == ' ' || value[value_len - 1] == '\t')) {
      value_len--;
    }
    if (!valid_value(value, value_len)) {
      return STARIO_H1_ERROR;
    }
    if (header_count >= STARIO_H1_MAX_HEADERS) {
      return STARIO_H1_INCOMPLETE;
    }
    headers[header_count].name = p;
    headers[header_count].name_len = name_len;
    headers[header_count].value = value;
    headers[header_count].value_len = value_len;
    header_count++;

    if (name_eq(p, name_len, "content-length", 14)) {
      uint64_t parsed = 0;
      if (parse_content_length(value, value_len, &parsed) != H1_OK) {
        return STARIO_H1_ERROR;
      }
      if (has_cl && parsed != content_length) {
        return STARIO_H1_ERROR;
      }
      has_cl = 1;
      content_length = parsed;
      flags |= F_CONTENT_LENGTH;
    } else if (name_eq(p, name_len, "transfer-encoding", 17)) {
      has_te = 1;
      flags |= F_TRANSFER_ENCODING;
      if (te_is_chunked(value, value_len)) {
        flags |= F_CHUNKED;
      }
    } else if (name_eq(p, name_len, "connection", 10)) {
      scan_connection(value, value_len, &flags);
    } else if (name_eq(p, name_len, "upgrade", 7)) {
      flags |= F_UPGRADE;
    } else if (name_eq(p, name_len, "expect", 6)) {
      if (is_expect_continue(value, value_len)) {
        expect_continue = 1;
      }
    }
    p = row_end + 2;
  }

  if (has_te && has_cl) {
    return STARIO_H1_ERROR;
  }
  if (has_te) {
    return STARIO_H1_INCOMPLETE;
  }
  if (http_method == HTTP_CONNECT) {
    return STARIO_H1_INCOMPLETE;
  }

  header_bytes = (size_t)(headers_term + 4 - data);
  if (has_cl) {
    if (content_length > (uint64_t)(SIZE_MAX - header_bytes)) {
      return STARIO_H1_ERROR;
    }
    total = header_bytes + (size_t)content_length;
  } else {
    total = header_bytes;
  }
  if (total > max_message) {
    return STARIO_H1_INCOMPLETE;
  }
  if (has_cl && header_bytes + (size_t)content_length > len) {
    return STARIO_H1_INCOMPLETE;
  }
  if (expect_continue && has_cl && content_length > 0 &&
      header_bytes + (size_t)content_length > len) {
    return STARIO_H1_INCOMPLETE;
  }

  parser->method = http_method;
  parser->http_major = 1;
  parser->http_minor = minor;
  parser->flags = flags;
  parser->content_length = has_cl ? content_length : 0;
  if ((flags & F_UPGRADE) && (flags & F_CONNECTION_UPGRADE)) {
    parser->upgrade = 1;
  } else {
    parser->upgrade = 0;
  }

  if (fire_cb(parser, settings->on_message_begin) != 0) {
    return STARIO_H1_ERROR;
  }
  if (fire_data(parser, settings->on_url, url, url_len) != 0) {
    return STARIO_H1_ERROR;
  }
  for (i = 0; i < header_count; i++) {
    if (fire_data(
            parser,
            settings->on_header_field,
            headers[i].name,
            headers[i].name_len
        ) != 0) {
      return STARIO_H1_ERROR;
    }
    if (fire_data(
            parser,
            settings->on_header_value,
            headers[i].value,
            headers[i].value_len
        ) != 0) {
      return STARIO_H1_ERROR;
    }
    if (fire_cb(parser, settings->on_header_value_complete) != 0) {
      return STARIO_H1_ERROR;
    }
  }
  if (fire_cb(parser, settings->on_headers_complete) != 0) {
    return STARIO_H1_ERROR;
  }
  if (has_cl && content_length > 0) {
    if (fire_data(
            parser,
            settings->on_body,
            data + header_bytes,
            (size_t)content_length
        ) != 0) {
      return STARIO_H1_ERROR;
    }
  }
  if (fire_cb(parser, settings->on_message_complete) != 0) {
    return STARIO_H1_ERROR;
  }
  return (int64_t)total;
}
