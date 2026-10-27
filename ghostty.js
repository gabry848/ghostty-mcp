// JXA bridge to Ghostty's AppleScript dictionary (Ghostty >= 1.3, macOS).
// Usage: osascript -l JavaScript ghostty.js '<json request>'
// The request is {"op": "...", ...}; the reply is JSON {"ok": ...} or {"error": "..."}.
ObjC.import('AppKit');

const g = Application('Ghostty');

function describe(term, extra) {
  const info = { id: term.id(), title: term.name(), cwd: term.workingDirectory() };
  try { info.tty = term.tty(); } catch (e) { /* tty arrives with Ghostty 1.4 */ }
  return Object.assign(info, extra || {});
}

function list() {
  if (!g.running()) return { version: null, frontmost: false, windows: [] };
  const windows = [];
  let front = null;
  try { front = g.frontWindow().id(); } catch (e) { /* no windows */ }
  g.windows().forEach(w => {
    const tabs = [];
    w.tabs().forEach(t => {
      let focused = null;
      try { focused = t.focusedTerminal().id(); } catch (e) { /* none */ }
      tabs.push({
        id: t.id(), title: t.name(), index: t.index(), selected: t.selected(),
        terminals: t.terminals().map(term => describe(term, { focused: term.id() === focused })),
      });
    });
    windows.push({ id: w.id(), title: w.name(), front: w.id() === front, tabs });
  });
  return { version: g.version(), frontmost: g.frontmost(), windows };
}

// Returns {term, tab, window} specifiers for a terminal id, or throws.
function locate(id) {
  for (const w of g.windows()) {
    for (const t of w.tabs()) {
      for (const term of t.terminals()) {
        if (term.id() === id) return { term, tab: t, window: w };
      }
    }
  }
  throw new Error(`terminal not found: ${id}`);
}

function surfaceConfig(c) {
  const cfg = g.newSurfaceConfiguration();
  if (!c) return cfg;
  if (c.cwd) cfg.initialWorkingDirectory = c.cwd;
  if (c.command) cfg.command = c.command;
  if (c.input) cfg.initialInput = c.input;
  if (c.env && c.env.length) cfg.environmentVariables = c.env;
  if (c.waitAfterCommand !== undefined) cfg.waitAfterCommand = c.waitAfterCommand;
  if (c.fontSize) cfg.fontSize = c.fontSize;
  return cfg;
}

function open(req) {
  const cfg = surfaceConfig(req.config);
  let term;
  if (req.where === 'window') {
    const w = g.newWindow({ withConfiguration: cfg });
    term = w.tabs()[0].focusedTerminal();
  } else if (req.where === 'tab') {
    const opts = { withConfiguration: cfg };
    if (req.target) opts.in = locate(req.target).window;
    if (!req.target && g.windows().length === 0) return open(Object.assign({}, req, { where: 'window' }));
    term = g.newTab(opts).focusedTerminal();
  } else if (req.where === 'split') {
    let base;
    if (req.target) base = locate(req.target).term;
    else base = g.frontWindow().selectedTab().focusedTerminal();
    term = g.split(base, { direction: req.direction || 'right', withConfiguration: cfg });
  } else {
    throw new Error(`unknown where: ${req.where}`);
  }
  return describe(term);
}

function close(req) {
  const loc = locate(req.id);
  if (req.scope === 'tab') g.closeTab(loc.tab);
  else if (req.scope === 'window') g.closeWindow(loc.window);
  else g.close(loc.term);
  return true;
}

function focus(req) {
  g.focus(locate(req.id).term);
  return true;
}

function input(req) {
  g.inputText(req.text, { to: locate(req.id).term });
  return true;
}

// keys: [{key, modifiers}] sent as press + release, delayMs apart.
function keys(req) {
  const term = locate(req.id).term;
  req.keys.forEach((k, i) => {
    if (i > 0 && req.delayMs) delay(req.delayMs / 1000);
    const opts = { to: term };
    if (k.modifiers) opts.modifiers = k.modifiers;
    g.sendKey(k.key, Object.assign({ action: 'press' }, opts));
    g.sendKey(k.key, Object.assign({ action: 'release' }, opts));
  });
  return true;
}

function action(req) {
  return g.performAction(req.action, { on: locate(req.id).term });
}

// Ghostty can only hand the screen file path over the clipboard,
// so save every pasteboard item, run the action, read the path and put the
// user's clipboard back exactly as it was.
function dump(req) {
  const term = locate(req.id).term;
  const pb = $.NSPasteboard.generalPasteboard;
  const saved = [];
  const items = pb.pasteboardItems;
  for (let i = 0; i < items.count; i++) {
    const item = items.objectAtIndex(i);
    const types = item.types;
    const entry = [];
    for (let j = 0; j < types.count; j++) {
      const type = types.objectAtIndex(j);
      const data = item.dataForType(type);
      if (data && !data.isNil()) entry.push([type, data]);
    }
    saved.push(entry);
  }
  const before = pb.changeCount;
  let path = null;
  try {
    g.performAction('write_screen_file:copy,plain', { on: term });
    for (let n = 0; n < 40 && pb.changeCount === before; n++) delay(0.05);
    if (pb.changeCount !== before) path = ObjC.unwrap(pb.stringForType($.NSPasteboardTypeString));
  } finally {
    pb.clearContents;
    if (saved.length) {
      const restored = $.NSMutableArray.array;
      saved.forEach(entry => {
        const item = $.NSPasteboardItem.alloc.init;
        entry.forEach(([type, data]) => item.setDataForType(data, type));
        restored.addObject(item);
      });
      pb.writeObjects(restored);
    }
  }
  if (!path) throw new Error('Ghostty did not write the screen file');
  return path;
}

const ops = { list, open, close, focus, input, keys, action, dump };

function run(argv) {
  try {
    const req = JSON.parse(argv[0]);
    const fn = ops[req.op];
    if (!fn) throw new Error(`unknown op: ${req.op}`);
    return JSON.stringify({ ok: fn(req) });
  } catch (e) {
    return JSON.stringify({ error: String(e && e.message || e) });
  }
}
