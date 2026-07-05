import './Header.css';

/**
 * Header
 * Top-of-page project identity bar.
 * Dark #1a2028 background with a subtle bottom border.
 */
function Header() {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <div className="site-header__brand">
          {/* Shield / wave icon mark */}
          <svg
            className="site-header__icon"
            viewBox="0 0 24 24"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
            aria-hidden="true"
          >
            <path
              d="M12 2L3 6v6c0 5.25 3.75 10.15 9 11.25C17.25 22.15 21 17.25 21 12V6L12 2z"
              fill="#7fa8c9"
              opacity="0.25"
            />
            <path
              d="M12 2L3 6v6c0 5.25 3.75 10.15 9 11.25C17.25 22.15 21 17.25 21 12V6L12 2z"
              stroke="#7fa8c9"
              strokeWidth="1.5"
              strokeLinejoin="round"
            />
            {/* wave detail */}
            <path
              d="M7 13c.8-.8 1.6-.8 2.4 0 .8.8 1.6.8 2.4 0 .8-.8 1.6-.8 2.4 0 .8.8 1.6.8 2.4 0"
              stroke="#7fa8c9"
              strokeWidth="1.2"
              strokeLinecap="round"
            />
          </svg>

          <div className="site-header__text">
            <h1 className="site-header__title">FloodGuard AI</h1>
            <p className="site-header__subtitle">
              Smart neighborhood flood resilience system
            </p>
          </div>
        </div>

        {/* Right-side badge */}
        <div className="site-header__track">
          <span className="site-header__track-label">Qwen Cloud · Global AI Hackathon</span>
          <span className="site-header__track-tag">Track 5 — EdgeAgent</span>
        </div>
      </div>
    </header>
  );
}

export default Header;
