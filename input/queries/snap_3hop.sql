SELECT * FROM account AS a1, txn AS t1, account AS a2, txn AS t2, account AS a3, txn AS t3, account AS a4
WHERE a1.account_id = t1.acc_from
  AND a2.account_id = t1.acc_to
  AND a2.account_id = t2.acc_from
  AND a3.account_id = t2.acc_to
  AND a3.account_id = t3.acc_from
  AND a4.account_id = t3.acc_to
  AND a1.account_id = 3569342;
