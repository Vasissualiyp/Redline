-- "Sync to Overleaf": push when Overleaf is unchanged since the last sync,
-- otherwise let an agent of your choice merge first, then confirm the push.
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}

--- Run a redline subcommand, streaming its output into a bottom split.
local function stream(args, title, on_exit)
  local st = core.state()
  vim.cmd("botright 14new")
  local buf = vim.api.nvim_get_current_buf()
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "wipe"
  vim.api.nvim_buf_set_name(buf, "redline://" .. title .. "-" .. buf)
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, { "$ redline " .. args, "" })
  local win = vim.api.nvim_get_current_win()
  vim.keymap.set("n", "q", "<cmd>close<CR>", { buffer = buf })
  vim.cmd("wincmd p")
  local function append(_, data)
    vim.schedule(function()
      if not vim.api.nvim_buf_is_valid(buf) then
        return
      end
      local lines = vim.tbl_filter(function(l) return l ~= "" end, data or {})
      if #lines > 0 then
        vim.api.nvim_buf_set_lines(buf, -1, -1, false, lines)
        if vim.api.nvim_win_is_valid(win) then
          vim.api.nvim_win_set_cursor(win, { vim.api.nvim_buf_line_count(buf), 0 })
        end
      end
    end)
  end
  vim.fn.jobstart(rpc.shell(args), {
    cwd = st.root,
    env = { REDLINE_ROOT = st.root },
    on_stdout = append,
    on_stderr = append,
    on_exit = function(_, code)
      vim.schedule(function()
        append(nil, { "", ("[exit %d]"):format(code) })
        core.reload()
        if on_exit then
          on_exit(code)
        end
      end)
    end,
  })
end

local function push()
  stream("sync push --no-pull", "push", function(code)
    vim.notify(code == 0 and "redline: pushed to Overleaf" or "redline: push refused or failed (see split)",
      code == 0 and vim.log.levels.INFO or vim.log.levels.WARN)
  end)
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
        if vim.fn.confirm("Overleaf is unchanged since the last sync. Push now?", "&Yes\n&No", 1) == 1 then
          push()
        end
        return
      end
      local summary = "Overleaf changed: " .. table.concat(chk.remote_changed, ", ")
      if #(chk.both_changed or {}) == 0 then
        if vim.fn.confirm(summary .. "\nOnly Overleaf touched these. Take them and push?", "&Yes\n&No", 1) == 1 then
          stream("sync overleaf --reuse-download --yes", "sync")
        end
        return
      end
      local clean = true
      for _, f in ipairs(chk.both_changed) do
        if (chk.conflicts[f] or 0) > 0 then
          clean = false
        end
      end
      local opts = {}
      for _, a in ipairs(chk.agents or {}) do
        table.insert(opts, { name = a, label = "merge with " .. a })
      end
      if clean then
        table.insert(opts, 1, { name = "git", label = "git: merges cleanly, no agent needed" })
      end
      local conflicts = {}
      for _, f in ipairs(chk.both_changed) do
        table.insert(conflicts, ("%s (%d conflict block(s))"):format(f, chk.conflicts[f] or 0))
      end
      vim.ui.select(opts, {
        prompt = summary .. "; both sides changed " .. table.concat(conflicts, ", ") .. ". Who merges?",
        format_item = function(o) return o.label end,
      }, function(choice)
        if not choice then
          return
        end
        stream("sync overleaf --reuse-download --no-push --agent " .. vim.fn.shellescape(choice.name), "merge",
          function(code)
            if code ~= 0 then
              return vim.notify("redline: merge not applied (see split)", vim.log.levels.WARN)
            end
            if vim.fn.confirm("Merged and reloaded. Review it, then push to Overleaf now?", "&Yes\n&No", 2) == 1 then
              push()
            end
          end)
      end)
    end))
end

return M
