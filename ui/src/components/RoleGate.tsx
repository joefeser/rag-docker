import { ReactNode } from 'react'
import { useRole } from '../context/RoleContext'

interface Props {
  roles: string[]
  children: ReactNode
}

export default function RoleGate({ roles, children }: Props) {
  const { role } = useRole()
  if (!role || !roles.includes(role)) return null
  return <>{children}</>
}
