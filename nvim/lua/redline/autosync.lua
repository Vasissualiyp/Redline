-- Single-editor papers (`single_editor = true` in .redline.toml): sync with Overleaf by itself.
--
-- Every edit (yours, or an agent's arriving from disk) restarts a countdown of auto_sync_seconds;
-- when it runs out `redline sync auto` runs in the background (nothing blocks): push if Overleaf is
-- unchanged, merge first when Overleaf is ahead or the merge is clean. Every auto_sync_poll seconds
-- it also looks for edits made on Overleaf itself. Conflicts, lint and compile errors are never
-- forced: a warning says what to do. The statusline shows an animated "syncing" while it runs.
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}
local state = { cfg = nil, cfg_at = 0, timer = nil, poll = nil, running = false, again = false, buf = nil, last = nil }

--- {enabled, seconds, poll}: asked from the CLI (so .redline.toml stays the single source), cached 30 s.
local function with_config(cb)
  local st = state.buf and core.bufs[state.buf]
  if not st then
    return
  end
  if state.cfg and vim.uv.now() - state.cfg_at < 30000 then
    return cb(state.cfg)
  end
  vim.system(rpc.shell("sync auto --enabled"), { cwd = st.root, text = true, env = { REDLINE_ROOT = st.root } },
    vim.schedule_wrap(function(r)
      local ok, cfg = pcall(vim.json.decode, r.stdout or "")
      state.cfg = ok and type(cfg) == "table" and cfg or { enabled = false, seconds = 20, poll = 120 }
      state.cfg_at = vim.uv.now()
      if state.cfg.enabled then
        M.start_poll()
      elseif state.poll then
        state.poll:stop()
      end
      cb(state.cfg)
    end))
end

local function run()
  local buf = state.buf
  local st = buf and core.bufs[buf]
  if not st then
    return
  end
  if state.running then
    state.again = true -- edits during a sync: one more round afterwards
    return
  end
  if vim.fn.mode():match("^[iR]") then
    return M.poke(buf) -- still typing
  end
  core.flush()
  state.running = true
  local stop_spin = require("redline.spinner").start("syncing")
  vim.system(rpc.shell("sync auto --json"), { cwd = st.root, text = true, env = { REDLINE_ROOT = st.root } },
    vim.schedule_wrap(function(r)
      stop_spin()
      state.running = false
      local ok, res = pcall(vim.json.decode, r.stdout or "")
      if ok and type(res) == "table" then
        if res.state == "pulled" or (res.state == "pushed" and (res.text or ""):find("merged")) then
          core.reload() -- Overleaf's edits are in the file now
          vim.notify("redline: took Overleaf's edits")
        elseif res.state == "conflict" or res.state == "blocked" then
          if state.last ~= res.state then
            vim.notify("redline: auto-sync needs you (" .. res.state .. "): "
              .. ((res.text or ""):match("[^\n]+") or ""), vim.log.levels.WARN)
          end
        end
        state.last = res.state
      elseif r.code ~= 5 and r.code ~= 4 then
        vim.notify("redline: auto-sync failed: " .. ((r.stderr or ""):match("[^\n]+") or "?"), vim.log.levels.WARN)
      end
      if state.again then
        state.again = false
        M.poke(buf)
      end
    end))
end

--- An edit happened: (re)start the countdown (only for single_editor papers).
function M.poke(buf)
  state.buf = buf or vim.api.nvim_get_current_buf()
  with_config(function(cfg)
    if not cfg.enabled then
      return
    end
    state.timer = state.timer or vim.uv.new_timer()
    state.timer:stop()
    state.timer:start(cfg.seconds * 1000, 0, vim.schedule_wrap(run))
  end)
end

function M.start_poll()
  if state.poll or not state.cfg then
    return
  end
  state.poll = vim.uv.new_timer()
  state.poll:start(state.cfg.poll * 1000, state.cfg.poll * 1000, vim.schedule_wrap(function()
    if state.cfg and state.cfg.enabled then
      run()
    end
  end))
end

--- Sync now (the poll and the countdown both end up here).
function M.now(buf)
  state.buf = buf or vim.api.nvim_get_current_buf()
  run()
end

return M
