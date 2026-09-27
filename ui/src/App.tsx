import { BrowserRouter } from 'react-router-dom'
import { RoleProvider } from './context/RoleContext'
import { QueryConfigProvider } from './context/QueryConfigContext'
import AppRouter from './router'

export default function App() {
  return (
    <BrowserRouter>
      <RoleProvider>
        <QueryConfigProvider>
          <AppRouter />
        </QueryConfigProvider>
      </RoleProvider>
    </BrowserRouter>
  )
}
