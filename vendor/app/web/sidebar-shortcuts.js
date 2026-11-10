/* Shared System/Projects sidebar: keep the native destinations and handlers. */
(() => {
  const group = document.getElementById('sidebar-shortcuts');
  if (!group) return;
  const menu = document.getElementById('sidebar-shortcuts-menu');
  const toggle = menu.querySelector('summary');
  const recent = document.getElementById('sidebar-last-shortcut');
  const items = [...group.querySelectorAll('[data-sidebar-shortcut]')];
  const storageKey = 'dex-sidebar-last-shortcut-v1';
  let selected = items[0];
  function select(item, persist = true) {
    if (!item) return;
    selected = item;
    const label = item.dataset.shortcutLabel;
    recent.querySelector('[data-shortcut-icon]').textContent = item.dataset.shortcutIcon;
    recent.querySelector('[data-shortcut-label]').textContent = label;
    recent.title = `Último atalho: ${label}`;
    recent.setAttribute('aria-label', `${label} — último atalho usado`);
    recent.dataset.shortcut = item.dataset.sidebarShortcut;
    if (persist) {
      try { localStorage.setItem(storageKey, item.dataset.sidebarShortcut); } catch {}
    }
  }
  try { selected = items.find(item => item.dataset.sidebarShortcut === localStorage.getItem(storageKey)) || selected; } catch {}
  select(selected, false);
  const close = (focus = false) => {
    menu.open = false;
    toggle.setAttribute('aria-expanded', 'false');
    if (focus) toggle.focus({preventScroll:true});
  };
  menu.addEventListener('toggle', () => toggle.setAttribute('aria-expanded', String(menu.open)));
  // Capture before existing document navigation handlers hide the drawer.
  document.addEventListener('click', event => {
    const item = event.target.closest('[data-sidebar-shortcut]');
    if (item && group.contains(item)) {
      select(item);
      // Hidden destination must not retain focus after the popup closes. The
      // destination's own handler may subsequently focus its panel/dialog.
      const restore = menu.contains(document.activeElement);
      close();
      if (restore) recent.focus({preventScroll:true});
    } else if (!group.contains(event.target)) close();
  }, true);
  recent.addEventListener('click', () => { close(); selected.click(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu.open) {
      event.preventDefault(); event.stopPropagation(); close(true);
    }
  }, true);
  group.addEventListener('focusout', event => {
    if (!group.contains(event.relatedTarget)) close();
  });
  document.getElementById('close-sidebar')?.addEventListener('click', () => close());
  window.addEventListener('storage', event => {
    if (event.key === storageKey) select(items.find(item => item.dataset.sidebarShortcut === event.newValue) || items[0], false);
  });
})();
