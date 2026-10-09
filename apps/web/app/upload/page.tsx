import { redirect } from 'next/navigation';

// 会议纪要已升级为主页（/），旧地址保留重定向避免书签失效。
export default function UploadPage() {
  redirect('/');
}
