import { createContext, useContext, useReducer, ReactNode } from 'react'

type Role = 'engineer' | 'developer' | 'end_user' | null

interface RoleState { role: Role }
type RoleAction = { type: 'SET_ROLE'; role: Role }

const RoleContext = createContext<{ role: Role; setRole: (r: Role) => void } | null>(null)

function reducer(_state: RoleState, action: RoleAction): RoleState {
  return { role: action.role }
}

function loadRole(): Role {
  try {
    const stored = sessionStorage.getItem('rag_role')
    if (!stored) return null
    const parsed = JSON.parse(stored)
    return parsed.role ?? null
  } catch {
    return null
  }
}

export function RoleProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, { role: loadRole() })

  function setRole(role: Role) {
    if (role) {
      sessionStorage.setItem('rag_role', JSON.stringify({ role }))
    } else {
      sessionStorage.removeItem('rag_role')
    }
    dispatch({ type: 'SET_ROLE', role })
  }

  return (
    <RoleContext.Provider value={{ role: state.role, setRole }}>
      {children}
    </RoleContext.Provider>
  )
}

export function useRole() {
  const ctx = useContext(RoleContext)
  if (!ctx) throw new Error('useRole must be used within RoleProvider')
  return ctx
}
