-- Initial drafting (`redline draft`): a brief (draft.toml), a lit review you check, then a first draft.
--   :Redline draft            status        :Redline draft init     scaffold draft.toml and open it
--   :Redline draft brief      open it       :Redline draft review   lit review (headless, slow)
--   :Redline draft approve    after you have read / edited draft/*.md
--   :Redline draft write      the first draft (paper) or survey.tex (survey)     :Redline draft run   review (+ write)
local core = require("redline")
local rpc = require("redline.rpc")
local M = {}

local SLOW = { review = "lit review", write = "first draft", run = "drafting" }

local function open_file(root, name)
  vim.cmd("edit " .. vim.fn.fnameescape(root .. "/" .. name))
end

function M.run(action)
  local st = core.state() or core.attach()
  if not st then return end
  local root = st.root
  if action == "brief" then return open_file(root, "draft.toml") end
  local args = "draft " .. action
  local done = SLOW[action] and require("redline.spinner").start(SLOW[action])
  vim.system(rpc.shell(args), { cwd = root, text = true, env = { REDLINE_ROOT = root } }, vim.schedule_wrap(function(r)
    if done then done() end
    local out = vim.trim((r.stdout or "") .. (r.stderr or ""))
    vim.notify("redline draft: " .. out, r.code == 0 and vim.log.levels.INFO or vim.log.levels.ERROR)
    if r.code ~= 0 then return end
    if action == "init" then open_file(root, "draft.toml") end
    if action == "review" or action == "run" then
      local f = io.open(root .. "/draft/litreview.md")
      if f then f:close(); open_file(root, "draft/litreview.md") end
    end
    if action == "write" or action == "run" then
      vim.cmd("checktime")
    end
  end))
end

return M
