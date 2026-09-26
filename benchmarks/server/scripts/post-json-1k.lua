local fixture = "benchmarks/server/fixtures/payload-1k.json"
local file = assert(io.open(fixture, "rb"))
wrk.method = "POST"
wrk.body = file:read("*a")
file:close()
wrk.headers["Content-Type"] = "application/json"
