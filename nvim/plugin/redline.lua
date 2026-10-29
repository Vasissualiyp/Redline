if vim.g.loaded_redline then
  return
end
vim.g.loaded_redline = true

local subcommands = {
  start = function() require("redline").attach() end,
  review = function() require("redline.review").toggle() end,
  list = function() require("redline.picker").pick() end,
  pending = function() require("redline.picker").pick(function(it) return it.marker == nil end) end,
  notes = function() require("redline.picker").pick(function(it) return it.marker ~= nil end) end,
  compile = function() require("redline.review").compile() end,
  fix = function() require("redline.review").fix_with_agent() end,
  push = function() require("redline.review").push() end,
  pull = function() require("redline.review").pull() end,
  acceptall = function() require("redline.review").accept_all() end,
  sync = function() require("redline.sync").sync() end,
  close = function() require("redline.sync").close() end,
  layout = function(args)
    if args[2] then require("redline.layout").apply(args[2]) else require("redline.layout").pick() end
  end,
  verify = function() require("redline.verify").request(false) end,
  verifyall = function() require("redline.verify").send_all() end,
  lint = function(args)
    local core = require("redline")
    local st = core.state() or core.attach()
    if args[2] == "fix" then
      local res, err = require("redline.rpc").call(st.root, "lint_fix", { text = core.buf_text(0) })
      if err then return vim.notify("redline: " .. err, vim.log.levels.ERROR) end
      core.apply_edits(0, res.edits)
      vim.notify(("redline: fixed %d issue(s)"):format(#res.fixed))
    end
    core.refresh(0, function() vim.diagnostic.setqflist({ namespace = require("redline.render").diag_ns }) end)
  end,
}

vim.api.nvim_create_user_command("Redline", function(o)
  local fn = subcommands[o.fargs[1] or "review"]
  if not fn then
    return vim.notify("Redline: unknown subcommand " .. o.fargs[1], vim.log.levels.ERROR)
  end
  fn(o.fargs)
end, {
  nargs = "*",
  complete = function(arg, line)
    if line:match("^%S+%s+lint%s") then
      return { "fix" }
    end
    if line:match("^%S+%s+layout%s") then
      return vim.tbl_map(function(p) return p.name end, require("redline.layout").presets())
    end
    return vim.tbl_filter(function(k) return vim.startswith(k, arg) end, vim.tbl_keys(subcommands))
  end,
  desc = "redline: review changes-package suggestions",
})
