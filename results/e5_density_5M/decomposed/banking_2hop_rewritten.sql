SELECT * FROM hop AS h1, hop AS h2
WHERE h1.account_dest_account_id = h2.account_src_account_id
  AND h1.account_src_account_id = 46;