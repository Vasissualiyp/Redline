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

function M.show(root, line, force)
  if not root then
    return
  end
  if not force and last == line then
    return
  end
  last = line
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
    local msg = vim.json.encode({ line = line, col = 0, force = force or nil }) .. "\n"
    pipe:write(msg, function()
      pipe:shutdown(function()
        pipe:close()
      end)
    end)
  end)
end

return M
