import React from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { vi, describe, it, expect, beforeEach } from 'vitest'
import App from './App' // We'll need to export LoginPage for testing
import * as api from './api'

vi.mock('./api', async () => {
  const actual = await vi.importActual('./api')
  return {
    ...actual,
    checkAuth: vi.fn(),
    login: vi.fn(),
  }
})

describe('LoginPage Error Handling', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.checkAuth).mockResolvedValue({ authenticated: false, required: true })
  })

  it('displays error message on 401 Unauthorized', async () => {
    vi.mocked(api.login).mockResolvedValue({
      ok: false,
      error: 'invalid password'
    })

    render(<App />)

    // Wait for auth state to move from 'checking' to 'login'
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Ключ администратора')).toBeInTheDocument()
    })

    const input = screen.getByPlaceholderText('Ключ администратора')
    const button = screen.getByText('Войти')

    fireEvent.change(input, { target: { value: 'wrong-password' } })
    fireEvent.click(button)

    await waitFor(() => {
      expect(screen.getByText('invalid password')).toBeInTheDocument()
    })

    expect(api.login).toHaveBeenCalledWith('wrong-password')
  })
})
