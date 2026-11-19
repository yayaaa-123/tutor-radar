import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import RadarPage from '../app/radar-page';
import '../app/radar.css';
import '../app/profile.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RadarPage />
  </StrictMode>,
);
