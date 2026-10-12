export const API = process.env.NEXT_PUBLIC_API_URL || (
  typeof window === 'undefined'
    ? 'http://localhost:8000'
    : `${window.location.protocol}//${window.location.hostname}:8000`
);
