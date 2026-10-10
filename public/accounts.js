// Account entry on the login screen and authenticated workspace.
function addAccountLink() {
  if (document.getElementById('data-agent-account-link')) return;
  const link = document.createElement('a');
  link.id = 'data-agent-account-link';
  link.href = '/account';
  link.textContent = '注册 / 恢复账号 / 账号管理';
  Object.assign(link.style, {position: 'fixed', bottom: '12px', right: '20px', zIndex: '1000', background: 'var(--background, white)', color: '#234ed8', padding: '8px 12px', borderRadius: '8px', fontSize: '14px'});
  document.body.appendChild(link);
}
new MutationObserver(addAccountLink).observe(document.documentElement, {childList: true, subtree: true});
addAccountLink();
