
// Dashboard logic for Buy My Shop – קורא ל-API של השרת כדי להציג סטטיסטיקות
document.addEventListener('DOMContentLoaded', () => {
    const totalClicksEl = document.getElementById('total-clicks');
    const totalSalesEl = document.getElementById('total-sales');
    const totalEarningsEl = document.getElementById('total-earnings');

    // נקודת בסיס ל-API – אפשר לעדכן ידנית אם צריך
    const API_BASE = window.BOTSHOP_API_BASE || 'https://botshop-production.up.railway.app';

    async function fetchFinanceMetrics() {
        try {
            const res = await fetch(`${API_BASE}/api/metrics/finance`);
            if (!res.ok) {
                console.warn('Failed to load finance metrics', res.status);
                return;
            }
            const data = await res.json();
            if (!data || !data.reserve) {
                return;
            }

            const reserve = data.reserve;

            // מכירות = מספר תשלומים מאושרים
            const approvedCount = reserve.approved_count || 0;
            const totalPayments = reserve.total_payments || 0;

            // לחיצות – הערכה: פי 3 מהתשלומים הכוללים (ניתן לשנות בהמשך/מקור אמיתי)
            const estimatedClicks = totalPayments * 3;

            // רווחים נטו לפי מאגר (net_amount)
            const totalNet = reserve.total_net || 0;

            if (totalClicksEl) {
                totalClicksEl.textContent = estimatedClicks.toString();
            }
            if (totalSalesEl) {
                totalSalesEl.textContent = approvedCount.toString();
            }
            if (totalEarningsEl) {
                totalEarningsEl.textContent = `${totalNet.toFixed(2)}₪`;
            }
        } catch (err) {
            console.error('Error fetching finance metrics', err);
        }
    }

    fetchFinanceMetrics();
    // רענון כל כמה דקות
    setInterval(fetchFinanceMetrics, 5 * 60 * 1000);
});
