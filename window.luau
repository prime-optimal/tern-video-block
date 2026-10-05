--!strict
-- tern-video-block: video files open in a video block, a terminal block running the player (tern_video_block.py: the
-- frames through the kitty graphics protocol, the sound through mpv), focused. Every way Tern opens a file comes here:
-- the Files pane, the palette, a drop, a link, `tern open`. Files Tern cannot read here (on another host) keep the
-- built-in open.

local VIDEO = {
	mp4 = true, m4v = true, mov = true, mkv = true, webm = true, avi = true, wmv = true, flv = true,
	mpg = true, mpeg = true, ts = true, m2ts = true, mts = true, ogv = true, ["3gp"] = true,
}

-- A shell word for any string, read the same by sh, bash, zsh and fish: single quotes, with ' and \ written outside
-- them (fish reads \' and \\ inside single quotes, the others do not).
local function quote(s: string): string
	local body = string.gsub(s, "['\\]", function(c: string): string
		if c == "'" then
			return "'\\''"
		end
		return "'\\\\'"
	end)
	return "'" .. body .. "'"
end

-- The video block the last preview opened: the next preview takes its place.
local preview: number? = nil

tern.route.open(function(req: OpenRequest, cx: WindowCx): RouteDecision?
	local ext = string.lower(string.match(req.path, "%.([%w]+)$") or "")
	if not VIDEO[ext] or not tern.fs.exists(req.path) then
		return nil
	end
	local launch = {
		cwd = string.match(req.path, "^(.*)/[^/]*$") or "/",
		command = "python3 " .. quote(tern.plugin.dir .. "/tern_video_block.py") .. " " .. quote(req.path),
	}
	local reveal = { focus = true }
	local target = cx.session:focused()
	local old: number? = nil
	if req.how == "preview" and preview ~= nil and cx.session:pane(preview) ~= nil then
		target, old = preview, preview
	end
	if req.how == "tab" or target == nil then
		cx.layout:new_tab(launch, reveal)
		return { handled = true }
	end
	local pane = cx.layout:split(target, if req.how == "below" then "down" else "right", launch, reveal)
	if old ~= nil then
		cx.layout:close(old)
	end
	if req.how == "preview" then
		preview = pane
	end
	return { handled = true }
end)
