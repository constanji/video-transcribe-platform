import './globals.css';
import './workspace.css';
import './platform.css';
import './ui.css';
import { AuthGate, AuthProvider } from './providers';
import MeetingFloatingBar from '../components/MeetingFloatingBar';

export default function Layout({children}:{children:React.ReactNode}) {
  return (
    <html lang="zh-CN">
      <body>
        <AuthProvider>
          <AuthGate>{children}</AuthGate>
          {/* 会议转录进行中时，任意页面底部显示悬浮控制条（会话运行时是全局单例）。 */}
          <MeetingFloatingBar />
        </AuthProvider>
      </body>
    </html>
  );
}
