-- Send cursor positions to the PDF pane (`redline preview`).
local M = {}

local last = nil

--- Same naming as redline.config.socket_path.
function M.socket(root, name)
  local real = vim.uv.fs_realpath(root) or root
  local dir = vim.env.XDG_RUNTIME_DIR
  if not dir or dir == "" then
    dir = "/tmp"
  end
  return ("%s/redline-%s-%s.sock"):format(dir, vim.fn.sha256(real):sub(1, 12), name)
end

--- Text the PDF pane should highlight for a change: what is typeset for it.
function M.excerpt(it)
  if it.kind == "replaced" then
    return (it.new or "") .. " " .. (it.old or "")
  end
  return it.new or it.old or it.comment or ""
end

--- Show the change under the cursor if there is one, else the cursor line.
function M.show_cursor(root, force)
  local core = require("redline")
  local pos = vim.api.nvim_win_get_cursor(0)
  local it = core.item_at(vim.api.nvim_get_current_buf(), pos[1] - 1, pos[2])
  if it then
    return M.show(root, it.lines[1], force, M.excerpt(it), it.lines[2])
  end
  M.show(root, pos[1], force, vim.api.nvim_get_current_line())
end

function M.show(root, line, force, text, end_line)
  if not root then
    return
  end
  local key = line .. "\0" .. (text or "")
  if not force and last == key then
    return
  end
  last = key
  local sock = M.socket(root, "preview")
  if not vim.uv.fs_stat(sock) then
    if force then
      vim.notify("redline: no PDF pane running (redline preview)", vim.log.levels.WARN)
    end
    return
  end
  local pipe = vim.uv.new_pipe(false)
  pipe:connect(sock, function(err)
    if err then
      pipe:close()
      return
    end
    local msg = vim.json.encode({ line = line, col = 0, force = force or nil, text = text,
      ["end"] = end_line }) .. "\n"
    pipe:write(msg, function()
      pipe:shutdown(function()
        pipe:close()
      end)
    end)
  end)
end

return M
