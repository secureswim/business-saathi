# Soundbox merchant experience

The merchant route `/` is a first-person counter experience. It keeps the existing
Business Saathi API and adds a locally rendered, rounded CSS 3D Soundbox. Drag
horizontally or use Rotate / Reset view. No external models, fonts or CDN are
required to render the page.

## Interaction

- Blue Saathi button (on the device or below the conversation): start talking;
  press again to stop. Space works when the page background has keyboard focus.
- Rear `+` / `−`: announcement volume. Rear replay: latest announcement, including
  the payment demo. “Phir se suniye”: latest business answer.
- Customer pays ₹120: **local simulation only**, with a visible demo receipt and
  spoken confirmation. No payment endpoint or ledger mutation. The QR graphic
  is illustrative and contains no UPI payment address.
- Example questions and the visible typing fallback use the real `/api/query`.
- Offers require explicit approval; Hindi and Hinglish yes/no are supported.
  The existing 15-second proposal timeout is preserved. Campaign execution and
  fast-forwarded outcomes remain simulated and are labelled in the experience.
- The browser needs speech-recognition support and microphone permission for
  voice input. If unavailable, type a question. Answers remain readable when
  audio cannot play. Microphone streams and audio contexts are released on stop.
- Listening feedback follows microphone amplitude. The speaking animation is a
  status indicator while audio is playing, not a measured speaker waveform.
- Reduced-motion preferences disable decorative animation.

## Reference and scope

The supplied presentation is project context: voice-first advice, context,
merchant approval and the learning loop. It does not authorize additional work.

The visual model follows the supplied Soundbox photographs: a tilted rounded QR
panel with white trim and cyan/navy surround, a tapered blue speaker housing,
side perforations, and rear display and controls. The “Merchant side” view button
reveals the controls; Reset view returns to the customer-facing QR panel.
The housing joins rounded perimeter rings with continuous triangulated surfaces,
including the top, bottom and curved corners. Each face uses an orthonormal 3D
transform so side views render correctly. No external model assets are required.
It is an illustrative concept,
not a manufacturing model or exact replica of a particular hardware revision.
The added Saathi button represents the hackathon enhancement.

Official references reviewed:
- https://business.paytm.com/blog/how-to-simplify-payments-in-your-shop-with-paytm-soundbox/
  Customer scans and pays; device announces the receipt; last payment replay.
- https://business.paytm.com/soundbox-devices
  Device appearance and product family.

## Validation

`node --check frontend/merchant.js`
`node --check frontend/soundbox.js`
`node --test tests/merchant-ui.test.cjs`

Use the root merchant page and `/ops` together for the hackathon presentation.
