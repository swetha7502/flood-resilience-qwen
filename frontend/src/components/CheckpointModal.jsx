import React, { useState } from 'react';
import './CheckpointModal.css';

/**
 * CheckpointModal Component
 * Shows a modal prompt for human approval actions.
 *
 * Props:
 *   activeCheckpoint {object} — current checkpoint item (e.g. from action_taken event)
 *   queueCount {number} — total checkpoints currently in queue
 *   onApprove {function} — callback to handle approval
 *   onDismiss {function} — callback to handle closing/dismissal
 */
function CheckpointModal({ activeCheckpoint, queueCount, onApprove, onDismiss }) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);

  if (!activeCheckpoint) return null;

  const { zone, payload } = activeCheckpoint;
  const message = payload?.message || 'Action requires authorization check.';
  const actionName = payload?.action || 'Unknown Action';
  const confidence = payload?.confidence ?? activeCheckpoint.confidence; // support payload or event-level confidence

  const handleApproveClick = async () => {
    setIsSubmitting(true);
    setErrorMsg(null);
    try {
      await onApprove(zone, activeCheckpoint);
    } catch (err) {
      setErrorMsg(err.message || 'Failed to approve checkpoint.');
      setIsSubmitting(false);
    }
  };

  return (
    <div className="checkpoint-modal-overlay">
      <div className="checkpoint-modal-card" role="dialog" aria-modal="true">
        {/* Modal Header */}
        <div className="checkpoint-modal-header">
          <div className="checkpoint-header-left">
            <span className="checkpoint-header-pulse-icon">⚠️</span>
            <h3 className="checkpoint-modal-title">Human Checkpoint Required</h3>
          </div>
          {queueCount > 1 && (
            <span className="checkpoint-queue-badge">
              1 of {queueCount} queued
            </span>
          )}
        </div>

        {/* Modal Body */}
        <div className="checkpoint-modal-body">
          <div className="checkpoint-detail-grid">
            <div className="checkpoint-detail-row">
              <span className="checkpoint-detail-label">Target Zone</span>
              <span className={`checkpoint-detail-val zone-text-${zone?.toLowerCase()}`}>
                Zone {zone}
              </span>
            </div>

            <div className="checkpoint-detail-row">
              <span className="checkpoint-detail-label">Requested Action</span>
              <span className="checkpoint-detail-val action-highlight">
                {actionName.replace(/_/g, ' ')}
              </span>
            </div>

            {typeof confidence === 'number' && (
              <div className="checkpoint-detail-row">
                <span className="checkpoint-detail-label">Confidence Score</span>
                <span className="checkpoint-detail-val confidence-score">
                  {(confidence * 100).toFixed(0)}%
                </span>
              </div>
            )}
          </div>

          <div className="checkpoint-message-container">
            <h4 className="checkpoint-message-heading">System Reasoning Message:</h4>
            <p className="checkpoint-message-text">{message}</p>
          </div>

          {errorMsg && (
            <div className="checkpoint-error-box">
              Error: {errorMsg}
            </div>
          )}
        </div>

        {/* Modal Actions */}
        <div className="checkpoint-modal-footer">
          <button
            className="checkpoint-btn-dismiss"
            onClick={onDismiss}
            disabled={isSubmitting}
          >
            Dismiss
          </button>
          <button
            className="checkpoint-btn-approve"
            onClick={handleApproveClick}
            disabled={isSubmitting}
          >
            {isSubmitting ? 'Approving…' : 'Approve Action'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default CheckpointModal;
