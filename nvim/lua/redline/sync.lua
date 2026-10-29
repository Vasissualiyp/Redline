-- "Sync to Overleaf": push when Overleaf has nothing new, merge with git when
-- there are no conflicts, and only for real conflicts let an agent of your
-- choice merge first. Output goes to one bottom split that never keeps focus.
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}

local out = { buf = nil, win = nil }

local function paper_win()
  local st = core.state()
  for _, w in ipairs(vim.api.nvim_list_wins()) do
    if core.bufs[vim.api.nvim_win_get_buf(w)] then
      return w
    end
  end
  return st and vim.api.nvim_get_current_win()
end

--- Close the sync output split and go back to the paper.
function M.close()
  if out.win and vim.api.nvim_win_is_valid(out.win) then
    vim.api.nvim_win_close(out.win, true)
  end
  out.win = nil
  local w = paper_win()
  if w and vim.api.nvim_win_is_valid(w) then
    vim.api.nvim_set_current_win(w)
  end
end

local function show_output(title)
  if not (out.buf and vim.api.nvim_buf_is_valid(out.buf)) then
    out.buf = vim.api.nvim_create_buf(false, true)
    vim.bo[out.buf].bufhidden = "hide"
    vim.api.nvim_buf_set_name(out.buf, "redline://sync")
    for _, k in ipairs({ "q", "<Esc>", "<CR>" }) do
      vim.keymap.set("n", k, M.close, { buffer = out.buf, nowait = true, desc = "redline: close sync output" })
    end
  end
  if not (out.win and vim.api.nvim_win_is_valid(out.win)) then
    local back = vim.api.nvim_get_current_win()
    vim.cmd("botright 12split")
    out.win = vim.api.nvim_get_current_win()
    vim.api.nvim_win_set_buf(out.win, out.buf)
    vim.wo[out.win].winfixheight = true
    vim.wo[out.win].statusline = " redline sync — q / <Esc> closes, focus stays on the paper"
    vim.api.nvim_set_current_win(back)
  end
  vim.api.nvim_buf_set_lines(out.buf, 0, -1, false, { "── " .. title .. " ──" })
end

local function append(lines)
  if not (out.buf and vim.api.nvim_buf_is_valid(out.buf)) then
    return
  end
  lines = vim.tbl_filter(function(l) return l ~= "" end, lines or {})
  if #lines == 0 then
    return
  end
  vim.api.nvim_buf_set_lines(out.buf, -1, -1, false, lines)
  if out.win and vim.api.nvim_win_is_valid(out.win) then
    vim.api.nvim_win_set_cursor(out.win, { vim.api.nvim_buf_line_count(out.buf), 0 })
  end
end

--- Run a redline subcommand; with `visible`, stream it into the split.
local function run(args, visible, on_exit)
  local st = core.state()
  local collected = {}
  if visible then
    show_output("redline " .. args)
  end
  local function on_data(_, data)
    vim.schedule(function()
      vim.list_extend(collected, data or {})
      if visible then
        append(data)
      end
    end)
  end
  vim.fn.jobstart(rpc.shell(args), {
    cwd = st.root,
    env = { REDLINE_ROOT = st.root },
    on_stdout = on_data,
    on_stderr = on_data,
    on_exit = function(_, code)
      vim.schedule(function()
        core.reload()
        if on_exit then
          on_exit(code, table.concat(collected, "\n"):gsub("%s+$", ""))
        end
      end)
    end,
  })
end

local function push()
  vim.notify("redline: pushing to Overleaf…")
  run("sync push --no-pull", false, function(code, text)
    if code == 0 then
      vim.notify("redline: pushed to Overleaf")
    else
      show_output("push refused or failed")
      append(vim.split(text, "\n"))
      vim.notify("redline: push refused or failed (see the split; q closes it)", vim.log.levels.WARN)
    end
  end)
end

local function confirm_push(msg)
  local yes = vim.fn.confirm(msg, "&Push\n&Not now", 1) == 1
  M.close()
  if yes then
    push()
  end
end

function M.sync()
  local st = core.state()
  if not st then
    return vim.notify("redline: buffer not attached")
  end
  core.flush()
  vim.notify("redline: checking Overleaf…")
  vim.system(rpc.shell("sync check --json"), { cwd = st.root, text = true, env = { REDLINE_ROOT = st.root } },
    vim.schedule_wrap(function(r)
      local ok, chk = pcall(vim.json.decode, r.stdout or "", { luanil = { object = true, array = true } })
      if not ok or type(chk) ~= "table" then
        return vim.notify("redline sync: " .. ((r.stderr or "") .. (r.stdout or "")), vim.log.levels.ERROR)
      end
      if chk.remote_unchanged then
        return confirm_push("Overleaf has nothing new since the last sync. Push your edits?")
      end
      local what = table.concat(chk.remote_changed, ", ")
      if chk.clean then
        -- Overleaf edits that merge without conflicts: no agent needed
        vim.notify("redline: merging Overleaf edits (" .. what .. ")…")
        return run("sync overleaf --reuse-download --no-push --agent git", false, function(code, text)
          if code ~= 0 then
            show_output("merge failed")
            append(vim.split(text, "\n"))
            return
          end
          confirm_push("Merged Overleaf's edits in " .. what .. " (no conflicts). Push now?")
        end)
      end
      local opts = {}
      for _, a in ipairs(chk.agents or {}) do
        table.insert(opts, a)
      end
      local blocks = {}
      for _, f in ipairs(chk.both_changed) do
        table.insert(blocks, ("%s: %d conflict block(s)"):format(f, chk.conflicts[f] or 0))
      end
      vim.ui.select(opts, {
        prompt = "Overleaf and your copy conflict (" .. table.concat(blocks, ", ") .. "). Who merges?",
      }, function(agent)
        if not agent then
          return
        end
        run("sync overleaf --reuse-download --no-push --agent " .. vim.fn.shellescape(agent), true,
          function(code)
            if code ~= 0 then
              return vim.notify("redline: merge not applied (see the split; q closes it)", vim.log.levels.WARN)
            end
            confirm_push("Merged and reloaded (agent notes in the split). Push now?")
          end)
      end)
    end))
end

return M
