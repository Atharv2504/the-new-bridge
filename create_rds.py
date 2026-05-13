import boto3

# Create RDS instance
rds = boto3.client('rds', region_name='us-east-1')

# Get default VPC
ec2 = boto3.client('ec2', region_name='us-east-1')
vpcs = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])
vpc_id = vpcs['Vpcs'][0]['VpcId']

# Get default subnet group or create one
try:
    subnet_groups = rds.describe_db_subnet_groups(DBSubnetGroupName='default')
    subnet_group = 'default'
except:
    # Get subnets in default VPC
    subnets = ec2.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])
    subnet_ids = [s['SubnetId'] for s in subnets['Subnets'][:2]]  # Take first 2
    rds.create_db_subnet_group(
        DBSubnetGroupName='bridgethegap-subnet',
        DBSubnetGroupDescription='Subnet group for BridgeTheGap',
        SubnetIds=subnet_ids
    )
    subnet_group = 'bridgethegap-subnet'

# Create security group
sg = ec2.create_security_group(
    GroupName='bridgethegap-db-sg',
    Description='Security group for BridgeTheGap DB',
    VpcId=vpc_id
)
sg_id = sg['GroupId']

# Allow PostgreSQL port
ec2.authorize_security_group_ingress(
    GroupId=sg_id,
    IpPermissions=[{
        'IpProtocol': 'tcp',
        'FromPort': 5432,
        'ToPort': 5432,
        'IpRanges': [{'CidrIp': '0.0.0.0/0'}]  # Allow all for demo
    }]
)

# Create DB instance
rds.create_db_instance(
    DBInstanceIdentifier='bridgethegap-db',
    DBInstanceClass='db.t3.micro',
    Engine='postgres',
    MasterUsername='postgres',
    MasterUserPassword='TempPass123!',
    AllocatedStorage=20,
    DBSubnetGroupName=subnet_group,
    VpcSecurityGroupIds=[sg_id],
    PubliclyAccessible=True  # For demo
)

print("RDS instance creation initiated. It may take a few minutes.")