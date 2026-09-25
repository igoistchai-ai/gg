-- sandbox_runner.lua
-- Usage: runlua sandbox_runner.lua <target.lua>
-- Executes target.lua inside a restricted, Roblox-stubbed sandbox,
-- tracing every local variable assignment and every print/warn call,
-- writing structured trace lines to stderr as:
--   TRACE_VAR|<line>|<name>|<type>|<value>
--   TRACE_OUT|<stream>|<value>
-- so the Python side can reconstruct real (executed) values.

local target = ...
if not target then
    io.stderr:write("FATAL|no target file given\n")
    os.exit(1)
end

-- ---------------------------------------------------------------
-- Serialize a value safely for the trace line (single-line, escaped)
-- ---------------------------------------------------------------
local function safe_repr(v, depth)
    depth = depth or 0
    local t = type(v)
    if t == "string" then
        if #v > 500 then v = v:sub(1, 500) .. "...<truncated>" end
        local esc = v:gsub("\\", "\\\\"):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("|", "\\p")
        return esc
    elseif t == "number" or t == "boolean" then
        return tostring(v)
    elseif t == "nil" then
        return "nil"
    elseif t == "table" then
        if depth >= 1 then return "table:<nested>" end
        local parts = {}
        local n = 0
        for k, val in pairs(v) do
            n = n + 1
            if n > 30 then parts[#parts+1] = "...more"; break end
            parts[#parts+1] = tostring(k) .. "=" .. safe_repr(val, depth + 1)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    elseif t == "function" then
        return "function:" .. tostring(v)
    else
        return t .. ":" .. tostring(v)
    end
end

-- ---------------------------------------------------------------
-- Build a restricted sandbox environment.
-- Keeps: string, table, math, bit32/bit, os.time/os.clock (safe subset),
--        basic functions (print, pairs, ipairs, tostring, tonumber, type,
--        select, unpack/table.unpack, error, pcall, xpcall, assert, setmetatable,
--        getmetatable, rawget, rawset, rawequal, rawlen)
-- Removes: io, dofile, loadfile, require, package, os.execute/os.remove/etc,
--          debug (re-added internally only for tracing, not exposed to script)
-- Adds: minimal Roblox-like stubs (game, workspace, Instance, task, wait,
--       Vector3, CFrame, Color3, UDim2, Enum, script) so executor-style
--       scripts don't crash immediately on undefined globals -- they just
--       become harmless no-ops.
-- ---------------------------------------------------------------

local function make_stub_instance(name)
    local inst
    inst = setmetatable({}, {
        __index = function(_, k)
            return function(...) return inst end
        end,
        __newindex = function(t, k, v) rawset(t, k, v) end,
        __tostring = function() return "Instance<" .. (name or "?") .. ">" end,
        __call = function(...) return inst end,
    })
    return inst
end

local function make_vector3()
    local mt = {}
    mt.__index = mt
    mt.__add = function(a,b) return setmetatable({x=a.x+b.x,y=a.y+b.y,z=a.z+b.z}, mt) end
    mt.__sub = function(a,b) return setmetatable({x=a.x-b.x,y=a.y-b.y,z=a.z-b.z}, mt) end
    mt.__tostring = function(v) return string.format("%s, %s, %s", tostring(v.x), tostring(v.y), tostring(v.z)) end
    local V = {}
    V.new = function(x,y,z) return setmetatable({x=x or 0,y=y or 0,z=z or 0}, mt) end
    V.zero = V.new(0,0,0)
    return V
end

local function build_sandbox()
    local env = {}

    -- safe stdlib
    env.string = string
    env.table = table
    env.math = math
    env.bit32 = bit32
    env.utf8 = utf8
    env.os = { time = os.time, clock = os.clock, date = os.date }

    env.print = print
    env.warn = print
    env.pairs = pairs
    env.ipairs = ipairs
    env.next = next
    env.tostring = tostring
    env.tonumber = tonumber
    env.type = type
    env.select = select
    env.unpack = table.unpack
    env.error = error
    env.pcall = pcall
    env.xpcall = xpcall
    env.assert = assert
    env.setmetatable = setmetatable
    env.getmetatable = getmetatable
    env.rawget = rawget
    env.rawset = rawset
    env.rawequal = rawequal
    env.rawlen = rawlen
    env._VERSION = _VERSION

    -- allow load() but force it to run inside this SAME sandboxed env,
    -- so loadstring()-based unpacking still gets deobfuscated instead of
    -- escaping the sandbox.
    env.load = function(chunk, chunkname, mode, subenv)
        local f, err = load(chunk, chunkname, "t", subenv or env)
        return f, err
    end
    env.loadstring = env.load

    -- executor-style globals -> harmless stubs, so scripts don't crash
    env.game = make_stub_instance("game")
    env.workspace = make_stub_instance("workspace")
    env.script = make_stub_instance("script")
    env.Instance = { new = function(cls) return make_stub_instance(cls) end }
    env.Vector3 = make_vector3()
    env.Vector2 = { new = function(x,y) return {x=x or 0, y=y or 0} end }
    env.CFrame = { new = function(...) return make_stub_instance("CFrame") end }
    env.Color3 = { new = function(r,g,b) return {r=r,g=g,b=b} end, fromRGB = function(r,g,b) return {r=r,g=g,b=b} end }
    env.UDim2 = { new = function(...) return {...} end }
    env.UDim = { new = function(...) return {...} end }
    env.Enum = setmetatable({}, { __index = function() return setmetatable({}, {__index = function() return "EnumItem" end}) end })
    env.wait = function(n) return n end
    env.task = {
        wait = function(n) return n end,
        spawn = function(f, ...) if type(f) == "function" then return pcall(f, ...) end end,
        defer = function(f, ...) if type(f) == "function" then return pcall(f, ...) end end,
        delay = function(n, f, ...) return nil end,
    }
    env.spawn = env.task.spawn
    env.delay = env.task.delay
    env.tick = os.clock
    env.typeof = function(v)
        local t = type(v)
        if t == "table" then
            local mt = getmetatable(v)
            return "table"
        end
        return t
    end
    env.getgenv = function() return env end
    env.getrenv = function() return env end
    env.getfenv = function() return env end
    env.setfenv = function() return true end
    env.identifyexecutor = function() return "SandboxDeobfuscator", "1.0" end
    env.syn = setmetatable({}, {__index = function() return function(...) end end})
    env.Drawing = { new = function() return make_stub_instance("Drawing") end }
    env.request = function() return { StatusCode = 200, Body = "" } end
    env.http_request = env.request
    env.writefile = function() end
    env.readfile = function() return "" end
    env.isfile = function() return false end
    env.hookfunction = function(a,b) return a end
    env.hookmetamethod = function(a,b,c) return nil end
    env.newcclosure = function(f) return f end
    env.getnamecallmethod = function() return "" end
    env.setreadonly = function() end
    env.getgc = function() return {} end
    env.getconnections = function() return {} end
    env.checkcaller = function() return true end
    env.setclipboard = function() end
    env._G = env
    env._ENV = env

    env.shared = {}

    return env
end

-- ---------------------------------------------------------------
-- Redirect print()/warn() output into our trace stream too, so the
-- Python side can separately show "program output" vs "decoded vars".
-- ---------------------------------------------------------------
local sandbox = build_sandbox()
local real_print = print
sandbox.print = function(...)
    local n = select("#", ...)
    local parts = {}
    for i = 1, n do
        parts[i] = safe_repr((select(i, ...)))
    end
    io.stderr:write("TRACE_OUT|print|" .. table.concat(parts, "\t") .. "\n")
end
sandbox.warn = function(...)
    local n = select("#", ...)
    local parts = {}
    for i = 1, n do
        parts[i] = safe_repr((select(i, ...)))
    end
    io.stderr:write("TRACE_OUT|warn|" .. table.concat(parts, "\t") .. "\n")
end

-- ---------------------------------------------------------------
-- Line hook: after each executed line, dump all locals visible in the
-- currently-running function frame (level 2 relative to the hook).
-- We only care about the OUTERMOST (main chunk) frame's locals, since
-- that's where obfuscators put their "final" decoded constants; but we
-- also walk a couple of frames up in case of nested do/end blocks.
-- ---------------------------------------------------------------
local seen_last = {}
local runner_source = debug.getinfo(1, "S").source  -- this file's own source

debug.sethook(function(_, line)
    -- Walk the call stack and trace every frame that is NOT this runner
    -- file itself. That covers the target chunk plus any nested
    -- load()/loadstring() chunks it creates internally (a very common
    -- obfuscation trick), without hardcoding stack depths.
    for depth = 2, 20 do
        local info = debug.getinfo(depth, "S")
        if not info then break end

        if info.source ~= runner_source then
            local i = 1
            while true do
                local name, value = debug.getlocal(depth, i)
                if not name then break end
                if name ~= "(temporary)" and name:sub(1, 1) ~= "(" then
                    local key = depth .. ":" .. name
                    local rep = safe_repr(value)
                    if seen_last[key] ~= rep then
                        seen_last[key] = rep
                        io.stderr:write(string.format(
                            "TRACE_VAR|%d|%s|%s|%s\n",
                            line, name, type(value), rep
                        ))
                    end
                end
                i = i + 1
            end
        end
    end
end, "l")

local chunk, err = loadfile(target, "t", sandbox)
if not chunk then
    io.stderr:write("FATAL|compile error: " .. tostring(err) .. "\n")
    os.exit(2)
end

local ok, rerr = pcall(chunk)
debug.sethook()

if not ok then
    io.stderr:write("FATAL|runtime error: " .. tostring(rerr) .. "\n")
    os.exit(3)
end

io.stderr:write("DONE|ok\n")
