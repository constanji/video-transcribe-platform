'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { AuthUser } from '../lib/auth';
import { useAuth } from '../app/providers';

type AdminUser = AuthUser & { last_login_at?: string | null; created_at?: string | null };

function timeLabel(value?: string | null) {
  if (!value) return '—';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

export default function UserManagement({ onToast }: { onToast: (message: string, kind?: 'success' | 'error') => void }) {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState('');
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [resetPassword, setResetPassword] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<AdminUser[]>('/api/admin/users')
      .then(setUsers)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(load, [load]);

  async function update(target: AdminUser, payload: { role?: string; status?: string }) {
    setBusy(true);
    try {
      await api<AdminUser>(`/api/admin/users/${target.id}`, { method: 'PUT', body: JSON.stringify(payload) });
      onToast('已更新', 'success');
      load();
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function resetPasswordSubmit() {
    if (!resetTarget) return;
    if (resetPassword.length < 8) {
      onToast('密码至少 8 位', 'error');
      return;
    }
    setBusy(true);
    try {
      await api(`/api/admin/users/${resetTarget.id}/reset-password`, { method: 'POST', body: JSON.stringify({ new_password: resetPassword }) });
      onToast(`已重置 ${resetTarget.username} 的密码，该用户所有设备已下线`, 'success');
      setResetTarget(null);
      setResetPassword('');
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  async function remove(target: AdminUser) {
    if (!window.confirm(`确定删除用户「${target.username}」吗？其名下的视频/会议需先清空。`)) return;
    setBusy(true);
    try {
      await api(`/api/admin/users/${target.id}`, { method: 'DELETE' });
      onToast('已删除用户', 'success');
      load();
    } catch (err) {
      onToast((err as Error).message, 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="user-management">
      {error ? <p className="inline-error">{error}</p> : null}
      <table className="user-table">
        <thead>
          <tr>
            <th>用户</th>
            <th>角色</th>
            <th>状态</th>
            <th>最近登录</th>
            <th>注册时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((item) => {
            const isSelf = me?.id === item.id;
            return (
              <tr key={item.id}>
                <td>
                  <strong>{item.display_name || item.username}</strong>
                  <small>@{item.username}{isSelf ? '（当前登录）' : ''}</small>
                </td>
                <td>
                  <select
                    value={item.role}
                    disabled={busy || isSelf}
                    onChange={(event) => update(item, { role: event.target.value })}
                  >
                    <option value="admin">管理员</option>
                    <option value="user">普通用户</option>
                  </select>
                </td>
                <td>
                  <span className={`user-status ${item.status}`}>{item.status === 'active' ? '正常' : '已禁用'}</span>
                </td>
                <td>{timeLabel(item.last_login_at)}</td>
                <td>{timeLabel(item.created_at)}</td>
                <td className="user-actions">
                  <button
                    type="button"
                    className="outline-button"
                    disabled={busy || isSelf}
                    onClick={() => update(item, { status: item.status === 'active' ? 'disabled' : 'active' })}
                  >
                    {item.status === 'active' ? '禁用' : '启用'}
                  </button>
                  <button type="button" className="outline-button" disabled={busy} onClick={() => setResetTarget(item)}>
                    重置密码
                  </button>
                  <button type="button" className="outline-button danger" disabled={busy || isSelf} onClick={() => remove(item)}>
                    删除
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {resetTarget ? (
        <div className="confirm-mask">
          <div className="confirm-box">
            <h3>重置 {resetTarget.username} 的密码</h3>
            <label className="field-stack">
              新密码（至少 8 位）
              <input
                type="password"
                value={resetPassword}
                onChange={(event) => setResetPassword(event.target.value)}
                autoFocus
              />
            </label>
            <p className="settings-hint">重置后该用户所有设备的登录状态都会失效。</p>
            <div className="detail-actions">
              <button type="button" className="save-settings" disabled={busy} onClick={resetPasswordSubmit}>
                {busy ? '提交中…' : '确认重置'}
              </button>
              <button type="button" className="outline-button" onClick={() => { setResetTarget(null); setResetPassword(''); }}>
                取消
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
