export const getToken  = (): string | null => localStorage.getItem('token');
export const getUser   = (): string | null => localStorage.getItem('username');

export const setAuth = (token: string, username: string): void => {
  localStorage.setItem('token', token);
  localStorage.setItem('username', username);
};

export const clearAuth = (): void => {
  localStorage.removeItem('token');
  localStorage.removeItem('username');
};

export const isLoggedIn = (): boolean => !!getToken();
