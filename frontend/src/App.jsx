import { Navigate, Route, Routes } from 'react-router-dom'
import Navbar from './components/Navbar'
import ProtectedRoute from './components/ProtectedRoute'
import { AuthProvider } from './context/AuthContext'
import HomePage from './pages/HomePage'
import LoginPage from './pages/LoginPage'
import NotFoundPage from './pages/NotFoundPage'
import ProfilePage from './pages/ProfilePage'
import RegisterPage from './pages/RegisterPage'
import RequestDetailPage from './pages/RequestDetailPage'
import RequestsPage from './pages/RequestsPage'
import ServicesPage from './pages/ServicesPage'

export default function App() {
  return (
    <AuthProvider>
      <Navbar />
      <main className="app-main">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/services" element={<ServicesPage />} />

          <Route element={<ProtectedRoute />}>
            <Route path="/requests" element={<RequestsPage />} />
            <Route path="/requests/:requestId" element={<RequestDetailPage />} />
            <Route path="/profile" element={<ProfilePage />} />
          </Route>

          <Route path="/404" element={<NotFoundPage />} />
          <Route path="*" element={<Navigate to="/404" replace />} />
        </Routes>
      </main>
    </AuthProvider>
  )
}
