local fixture = "benchmarks/server/fixtures/payload-2m.bin"
local file = assert(io.open(fixture, "rb"))
local content = file:read("*a")
file:close()

local boundary = "----WebKitFormBoundaryBenchUpload"
local crlf = "\r\n"
local body = "--" .. boundary .. crlf
    .. 'Content-Disposition: form-data; name="file"; filename="payload-2m.bin"' .. crlf
    .. "Content-Type: application/octet-stream" .. crlf .. crlf
    .. content .. crlf
    .. "--" .. boundary .. "--"

wrk.method = "POST"
wrk.body = body
wrk.headers["Content-Type"] = "multipart/form-data; boundary=" .. boundary
