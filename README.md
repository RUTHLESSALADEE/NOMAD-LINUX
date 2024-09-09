One stop shop to manage Network Interface Cards (NICs) in Windows. 

Tabs:
  NIC Details - allows you to modify IP address and related settings or switch a NIC from static to DHCP. Additionally lets you modfiy MTU in the GUI since this feature is no longer available in Windows. 
  Routing Table - gives a GUI to the Windows routing commands. Based on NETSH, allows you to view, add, and delete routes from the Windows routing table. Only supports IPV4. 
  MTU - will display the MTU of each interface and has an MTU optimization tool. To use the tool input the maximum and minimum MTU you would like to test in addition to the address of the remote host. Timeout is optional and will default to 2000 ms if left blank. 

The program is set to request elevation since any commands that modify the routing table or a NIC are required to be run as an administrator. 

NIC Tab:

![image](https://github.com/user-attachments/assets/e37cd314-97d2-4cba-8dd0-3faa197ae232)

Routing Table Tab:

![image](https://github.com/user-attachments/assets/ce43098e-7e4b-420a-afb9-679cb0687f4b)

MTU Tab:

![image](https://github.com/user-attachments/assets/d9809bb0-f1e3-4d29-86b7-0aec12695b8b)


Happy toubleshooting!

