-- Vary path, query, and X-Request-Id together so the case cannot collapse
-- to one static URL. Working set is 4096 distinct param paths.
-- Reads: path param user_id, query q, header x-request-id.
local n = 4096
local i = 0

request = function()
  i = i % n + 1
  wrk.headers["X-Request-Id"] = "h" .. i
  return wrk.format(nil, "/user/" .. i .. "?q=term" .. i)
end
