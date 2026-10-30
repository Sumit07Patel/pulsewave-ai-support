-- Run once in Supabase SQL Editor, after schema.sql and seed.sql.
-- Links each product to the manual key used at ingestion (lower-cased file name without extension),
-- so a serial number can be traced to the right manual.

alter table products add column if not exists manual_product text;

-- Products that have a manual in the "manuals" bucket
insert into products (sku, name, warranty_months) values
  ('SPARK', 'Stone Spark',     12),
  ('STORM', 'StormCall Plus',  12)
on conflict do nothing;

update products set manual_product = 'airdopesapex'  where sku = 'APEX';
update products set manual_product = 'stonespark'    where sku = 'SPARK';
update products set manual_product = 'stormcallplus' where sku = 'STORM';
-- LITE and SPORT stay NULL: they have no manual yet, so questions about them search all manuals.

-- Sample devices so every manual has something to test against
insert into purchases (serial_number, order_id, sku, customer_id, purchase_date) values
  ('PW-SPK-40001', 'ORD-100520', 'SPARK', (select id from customers where email = 'diya@example.com'),  current_date - interval '5 months'),
  ('PW-STM-50001', 'ORD-100521', 'STORM', (select id from customers where email = 'kabir@example.com'), current_date - interval '15 months')
on conflict do nothing;
