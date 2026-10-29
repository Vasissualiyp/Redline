-- Close the whole redline tmux session (paper, agent, PDF, code), after asking.
local core = require("redline")
local rpc = require("redline.rpc")

local M = {}

function M.quit()
  core.flush()
  -- async: the CLI asks this nvim to save through its socket
  vim.system(rpc.shell("quit --status --json"), { text = true }, vim.schedule_wrap(function(r)
    local ok, st = pcall(vim.json.decode, r.stdout or "")
    if not ok or type(st) ~= "table" then
      return vim.notify("redline quit: " .. ((r.stderr or "") .. (r.stdout or "")), vim.log.levels.ERROR)
    end
    local lines = { ("Close the redline session? %d windows: %s"):format(#st.windows, table.concat(st.windows, ", ")) }
    if #st.unpushed > 0 then
      table.insert(lines, "Not on Overleaf yet: " .. table.concat(st.unpushed, ", "))
    else
      table.insert(lines, "Everything is on Overleaf.")
    end
    if st.agent_running then
      table.insert(lines, "A merge agent is still running.")
    end
    local choices = #st.unpushed > 0 and "&Quit everything\n&Sync to Overleaf first\n&Cancel"
      or "&Quit everything\n&Cancel"
    local pick = vim.fn.confirm(table.concat(lines, "\n"), choices, #st.unpushed > 0 and 3 or 2)
    if pick == 1 then
      core.flush()
      vim.fn.jobstart(rpc.shell("quit --yes"), { detach = true })
    elseif pick == 2 and #st.unpushed > 0 then
      require("redline.sync").sync()
    end
  end))
end

return M
