-- JSON-lines client for `redline rpc` (one long-running process per root).
local M = {}

local jobs = {} -- root -> { id, pending, next, partial }

local function base_cmd()
  local c = vim.env.REDLINE_CMD
  if c and c ~= "" then
    return c
  end
  return "redline"
end

--- Shell command prefix for running redline subcommands.
function M.shell(args)
  return { "sh", "-c", base_cmd() .. " " .. args }
end

local function on_line(job, line)
  if line == "" then
    return
  end
  local ok, msg = pcall(vim.json.decode, line, { luanil = { object = true, array = true } })
  if not ok or type(msg) ~= "table" then
    return
  end
  local entry = job.pending[msg.id]
  job.pending[msg.id] = nil
  if not entry then
    return
  end
  if entry.sync then
    entry.cb(msg.result, msg.error) -- only stores the result; safe in a fast context
  else
    vim.schedule(function()
      entry.cb(msg.result, msg.error)
    end)
  end
end

local function start(root)
  local job = { pending = {}, next = 0, partial = "" }
  job.id = vim.fn.jobstart(M.shell("rpc"), {
    cwd = root,
    env = { REDLINE_ROOT = root },
    on_stdout = function(_, data)
      data[1] = job.partial .. data[1]
      job.partial = table.remove(data)
      for _, l in ipairs(data) do
        on_line(job, l)
      end
    end,
    on_stderr = function(_, data)
      local msg = table.concat(data, "\n"):gsub("%s+$", "")
      if msg ~= "" then
        vim.schedule(function()
          vim.notify("redline rpc: " .. msg, vim.log.levels.WARN)
        end)
      end
    end,
    on_exit = function()
      jobs[root] = nil
      for _, entry in pairs(job.pending) do
        vim.schedule(function()
          entry.cb(nil, "redline rpc exited")
        end)
      end
    end,
  })
  if job.id <= 0 then
    error("could not start `" .. base_cmd() .. " rpc`; is redline installed or REDLINE_CMD set?")
  end
  jobs[root] = job
  return job
end

function M.request(root, method, params, cb, sync)
  local job = jobs[root] or start(root)
  job.next = job.next + 1
  job.pending[job.next] = { cb = cb, sync = sync }
  vim.fn.chansend(job.id, vim.json.encode({ id = job.next, method = method, params = params }) .. "\n")
end

--- Blocking variant (used for small, interactive operations).
function M.call(root, method, params, timeout)
  local done, res, err = false, nil, nil
  M.request(root, method, params, function(r, e)
    res, err, done = r, e, true
  end, true)
  vim.wait(timeout or 10000, function()
    return done
  end, 10)
  if not done then
    return nil, "redline rpc timed out"
  end
  return res, err
end

function M.stop_all()
  for _, job in pairs(jobs) do
    pcall(vim.fn.jobstop, job.id)
  end
  jobs = {}
end

return M
