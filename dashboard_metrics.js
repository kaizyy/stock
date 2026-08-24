(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.StockroomMetrics = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  const value = transaction => Number(transaction.qty || 0) * Number(transaction.price || 0);
  const sum = transactions => transactions.reduce((total, transaction) => total + value(transaction), 0);

  function isOlderThanDays(transaction, days, now = Date.now()) {
    const timestamp = new Date(transaction.date).getTime();
    return Number.isFinite(timestamp) && now - timestamp > days * 24 * 60 * 60 * 1000;
  }

  function isLowStock(item) {
    const minimum = Number(item.minStock || 0);
    return minimum > 0 && Number(item.stock || 0) <= minimum;
  }

  function stockAfterTransactionRemoval(currentStock, transaction) {
    const stock = Number(currentStock || 0);
    const quantity = Number(transaction.qty || 0);
    if (transaction.type === 'outgoing') return stock + quantity;
    if (transaction.type === 'incoming' && transaction.done) return stock - quantity;
    return stock;
  }

  function overview(state, now = Date.now()) {
    const outgoing = state.transactions.filter(transaction => transaction.type === 'outgoing');
    const paidIncoming = state.transactions.filter(transaction => transaction.type === 'incoming' && transaction.paid);
    const outstanding = outgoing.filter(transaction => !transaction.done);
    const overdue = outstanding.filter(transaction => isOlderThanDays(transaction, 7, now));
    const recent = outstanding.filter(transaction => !isOlderThanDays(transaction, 7, now));
    const expected = state.transactions.filter(transaction => transaction.type === 'incoming' && !transaction.done);
    const expectedPaid = expected.filter(transaction => transaction.paid);
    const expectedUnpaid = expected.filter(transaction => !transaction.paid);
    return {
      revenue: sum(outgoing) - sum(paidIncoming),
      outstanding,
      outstandingTotal: sum(outstanding),
      overdueTotal: sum(overdue),
      recentTotal: sum(recent),
      expected,
      expectedTotal: sum(expected),
      expectedPaidTotal: sum(expectedPaid),
      expectedUnpaidTotal: sum(expectedUnpaid),
      expectedUnits: expected.reduce((total, transaction) => total + Number(transaction.qty || 0), 0),
    };
  }

  return {overview, isLowStock, isOlderThanDays, stockAfterTransactionRemoval};
});
