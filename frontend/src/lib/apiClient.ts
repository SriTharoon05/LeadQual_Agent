import axios from 'axios';

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/$/, '');

export const apiClient = axios.create({
  baseURL: BASE_URL,
  timeout: 60_000,
});

// Attach Bearer token to every request
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401 clear storage and redirect to login
let isRedirecting = false;

apiClient.interceptors.response.use(
  (res) => res,
  (err) => {
    // A rejected password is a normal login error. Redirecting here used to
    // reload the form, erase its error and make sign-in failures look random.
    const isLogin = /(?:^|\/)auth\/login(?:\?|$)/.test(err.config?.url ?? '');
    const sentAuthorization = err.config?.headers?.Authorization;
    const currentToken = localStorage.getItem('token');
    const belongsToCurrentSession = typeof sentAuthorization === 'string'
      && currentToken !== null && sentAuthorization === `Bearer ${currentToken}`;
    if (err.response?.status === 401 && !isLogin && belongsToCurrentSession && !isRedirecting) {
      isRedirecting = true;
      localStorage.removeItem('token');
      localStorage.removeItem('username');
      // Use replace instead of href to avoid showing raw JSON
      window.location.replace('/login');
    }
    return Promise.reject(err);
  },
);
