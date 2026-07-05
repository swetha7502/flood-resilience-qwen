import './CloudBanner.css';

/**
 * CloudBanner
 *
 * Shows Qwen cloud connectivity status.
 * - green + left border when cloud_available is true
 * - red  + left border when cloud_available is false
 *
 * Props:
 *   cloudAvailable {boolean | null} - null = still loading from /health
 */
function CloudBanner({ cloudAvailable, onToggleCloud, isTogglingCloud }) {
  if (cloudAvailable === null) {
    return (
      <div className="cloud-banner cloud-banner--loading" role="status" aria-live="polite">
        <span className="cloud-banner__dot cloud-banner__dot--loading" />
        <span className="cloud-banner__text">Checking Qwen cloud status...</span>
      </div>
    );
  }

  const online = cloudAvailable === true;

  return (
    <div
      className={`cloud-banner ${online ? 'cloud-banner--online' : 'cloud-banner--offline'}`}
      role="status"
      aria-live="polite"
    >
      <span className={`cloud-banner__dot ${online ? 'cloud-banner__dot--online' : 'cloud-banner__dot--offline'}`} />

      <span className="cloud-banner__label">Qwen Cloud:</span>

      {online ? (
        <span className="cloud-banner__status">CONNECTED</span>
      ) : (
        <>
          <span className="cloud-banner__status">OFFLINE</span>
          <span className="cloud-banner__sub">- local rules active</span>
        </>
      )}

      {onToggleCloud && (
        <button
          type="button"
          className="cloud-banner__toggle"
          onClick={onToggleCloud}
          disabled={isTogglingCloud}
          aria-pressed={online}
          aria-label={online ? 'Disable cloud relay' : 'Enable cloud relay'}
        >
          <span className={`cloud-banner__toggle-track ${online ? 'cloud-banner__toggle-track--on' : 'cloud-banner__toggle-track--off'}`}>
            <span className="cloud-banner__toggle-thumb" />
          </span>
          <span className="cloud-banner__toggle-text">{online ? 'Cloud relay ON' : 'Cloud relay OFF'}</span>
        </button>
      )}
    </div>
  );
}

export default CloudBanner;
