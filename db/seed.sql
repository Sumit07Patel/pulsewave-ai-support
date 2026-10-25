-- Sample data for testing. Run after schema.sql.
insert into products (sku, name, warranty_months) values
  ('APEX',  'Airdopes Apex', 12),
  ('LITE',  'Airdopes Lite', 12),
  ('SPORT', 'Pulse Sport',   12)
on conflict do nothing;

insert into customers (name, email, phone) values
  ('Aarav Shah', 'aarav@example.com', '9000000001'),
  ('Diya Patel', 'diya@example.com',  '9000000002'),
  ('Kabir Mehta','kabir@example.com', '9000000003')
on conflict do nothing;

insert into purchases (serial_number, order_id, sku, customer_id, purchase_date) values
  ('PW-APX-10001','ORD-100234','APEX', (select id from customers where email='aarav@example.com'),  current_date - interval '3 months'),
  ('PW-APX-10002','ORD-100235','APEX', (select id from customers where email='diya@example.com'),   current_date - interval '20 months'),
  ('PW-LTE-20001','ORD-100301','LITE', (select id from customers where email='kabir@example.com'),  current_date - interval '10 months'),
  ('PW-SPT-30001','ORD-100412','SPORT',(select id from customers where email='aarav@example.com'),  current_date - interval '14 months')
on conflict do nothing;

-- Template for adding your own purchase:
-- insert into purchases (serial_number, order_id, sku, customer_id, purchase_date)
-- values ('PW-APX-10003','ORD-100500','APEX',(select id from customers where email='someone@example.com'),'2026-10-01');
