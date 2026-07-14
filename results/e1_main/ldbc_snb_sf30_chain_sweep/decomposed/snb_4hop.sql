SELECT * FROM hop AS h1, hop AS h2, hop AS h3, hop AS h4
WHERE h1.account_dest_account_id = h2.account_src_account_id
  AND h2.account_dest_account_id = h3.account_src_account_id
  AND h3.account_dest_account_id = h4.account_src_account_id
  AND h1.account_src_account_id = 22701;