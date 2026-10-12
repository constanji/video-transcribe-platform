import './globals.css';
import './workspace.css';
import './platform.css';
import './ui.css';
import { AuthGate, AuthProvider } from './providers';

export default function Layout({children}:{children:React.ReactNode}) {
  return (
    <html lang="zh-CN">
      <body>
        <AuthProvider>
          <AuthGate>{children}</AuthGate>
        </AuthProvider>
      </body>
    </html>
  );
}
