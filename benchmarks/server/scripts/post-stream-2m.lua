-- Same payload as buffered 2MB ingest; server route uses streaming read API.
local fixture = "benchmarks/server/fixtures/payload-2m.bin"
local file = assert(io.open(fixture, "rb"))
wrk.method = "POST"
wrk.body = file:read("*a")
file:close()
wrk.headers["Content-Type"] = "application/octet-stream"
