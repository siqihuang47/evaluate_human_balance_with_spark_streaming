from pyspark.sql import SparkSession
from pyspark.sql.functions import from_json, to_json, col, unbase64, base64, split, expr
from pyspark.sql.types import StructField, StructType, StringType, BooleanType, ArrayType, DateType, DoubleType

# Create a StructType for the Kafka redis-server topic which has all changes made to Redis - before Spark 3.0.0, schema inference is not automatic
kafkaRedisSchema = StructType(
    [
        StructField("key", StringType()),
        StructField("existType", StringType()),
        StructField("ch", BooleanType()),
        StructField("incr", BooleanType()),
        StructField("zSetEntries", ArrayType(
            StructType([
                StructField("element", StringType()),
                StructField("Score", DoubleType())
            ]))
        )
    ]
)

# Create a StructType for the Customer JSON that comes from Redis- before Spark 3.0.0, schema inference is not automatic
kafkaCustomerJSONSchema = StructType([
    StructField("customerName", StringType()),
    StructField("email", StringType()),
    StructField("phone", StringType()),
    StructField("birthDay", StringType())
])

# Create a StructType for the Kafka stedi-events topic which has the Customer Risk JSON that comes from Redis- before Spark 3.0.0, schema inference is not automatic
kafkaEventSchema = StructType(
    [
        StructField("customer", StringType()),
        StructField("score", DoubleType()),
        StructField("riskDate", DateType())
    ]
)

# Ceate a spark application object.
stediApp = SparkSession.builder.appName("STEDI").getOrCreate()

# Set the spark log level to WARN
stediApp.sparkContext.setLogLevel('WARN')

# Using the spark application object, read a streaming dataframe from the Kafka topic redis-server as the source
# Be sure to specify the option that reads all the events from the topic including those that were published before you started the spark stream
kafkaRedisDF = stediApp.readStream.format("kafka")\
    .option("kafka.bootstrap.servers", "localhost:9092")\
    .option("subscribe", "redis-server")\
    .option("startingOffsets", "earliest")\
    .load()

# Cast the value column in the streaming dataframe as a STRING
kafkaRedisDF = kafkaRedisDF.selectExpr("CAST(value AS string) value")

# Parse the single column "value" with a json object in it, like this:
# +------------+
# | value      |
# +------------+
# |{"key":"Q3..|
# +------------+
#
# with this JSON format: {"key":"Q3VzdG9tZXI=",
# "existType":"NONE",
# "Ch":false,
# "Incr":false,
# "zSetEntries":[{
# "element":"eyJjdXN0b21lck5hbWUiOiJTYW0gVGVzdCIsImVtYWlsIjoic2FtLnRlc3RAdGVzdC5jb20iLCJwaG9uZSI6IjgwMTU1NTEyMTIiLCJiaXJ0aERheSI6IjIwMDEtMDEtMDMifQ==",
# "Score":0.0
# }],
# "zsetEntries":[{
# "element":"eyJjdXN0b21lck5hbWUiOiJTYW0gVGVzdCIsImVtYWlsIjoic2FtLnRlc3RAdGVzdC5jb20iLCJwaG9uZSI6IjgwMTU1NTEyMTIiLCJiaXJ0aERheSI6IjIwMDEtMDEtMDMifQ==",
# "score":0.0
# }]
# }
#
# (Note: The Redis Source for Kafka has redundant fields zSetEntries and zsetentries, only one should be parsed)
#
# and create separated fields like this:
# +------------+-----+-----------+------------+---------+-----+-----+-----------------+
# |         key|value|expiredType|expiredValue|existType|   ch| incr|      zSetEntries|
# +------------+-----+-----------+------------+---------+-----+-----+-----------------+
# |U29ydGVkU2V0| null|       null|        null|     NONE|false|false|[[dGVzdDI=, 0.0]]|
# +------------+-----+-----------+------------+---------+-----+-----+-----------------+
#
# storing them in a temporary view called RedisSortedSet
kafkaRedisDF.withColumn("value", from_json("value", kafkaRedisSchema))\
            .select(col('value.existType'), col('value.ch'), col('value.incr'), col('value.zSetEntries'))\
            .createOrReplaceTempView("RedisSortedSet")

# Execute a sql statement against a temporary view, which statement takes the element field from the 0th element in the array of structs and create a column called encodedCustomer
# the reason we do it this way is that the syntax available select against a view is different than a dataframe, and it makes it easy to select the nth element of an array in a sql column
zSetEntriesEncodedDF = stediApp.sql(
    "SELECT zSetEntries[0].element AS encodedCustomer FROM RedisSortedSet")

# Take the encodedCustomer column which is base64 encoded at first like this:
# +--------------------+
# |            customer|
# +--------------------+
# |[7B 22 73 74 61 7...|
# +--------------------+

# and convert it to clear json like this:
# +--------------------+
# |            customer|
# +--------------------+
# |{"customerName":"...|
# +--------------------+
#
# with this JSON format: {"customerName":"Sam Test","email":"sam.test@test.com","phone":"8015551212","birthDay":"2001-01-03"}
zSetEntriesEncodedDF = zSetEntriesEncodedDF.withColumn(
    "customer", unbase64(col("encodedCustomer").cast("string")))

# Parse the JSON in the Customer record and store in a temporary view called CustomerRecords
zSetEntriesEncodedDF.withColumn("customer", from_json("customer", kafkaCustomerJSONSchema))\
                    .select(col('customer.*'))\
                    .createOrReplaceTempView("CustomerRecords")

# JSON parsing will set non-existent fields to null, so let's select just the fields we want, where they are not null as a new dataframe called emailAndBirthDayStreamingDF
emailAndBirthDayStreamingDF = stediApp.sql(
    " SELECT * FROM CustomerRecords WHERE email IS NOT NULL AND birthDay IS NOT NULL")

# Split the birth year as a separate field from the birthday
emailAndBirthDayStreamingDF = emailAndBirthDayStreamingDF.withColumn(
    'birthYear', split(col("birthDay"), "-").getItem(0))

# Select only the birth year and email fields as a new streaming data frame called emailAndBirthYearStreamingDF
emailAndBirthYearStreamingDF = emailAndBirthDayStreamingDF.select(
    'birthYear', 'email')

# Sink the emailAndBirthYearStreamingDF dataframe to the console in append mode
#
# The output should look like this:
# +--------------------+-----
# | email         |birthYear|
# +--------------------+-----
# |Gail.Spencer@test...|1963|
# |Craig.Lincoln@tes...|1962|
# |  Edward.Wu@test.com|1961|
# |Santosh.Phillips@...|1960|
# |Sarah.Lincoln@tes...|1959|
# |Sean.Howard@test.com|1958|
# |Sarah.Clark@test.com|1957|
# +--------------------+-----

# Run the python script by running the command from the terminal:
# /home/workspace/submit-redis-kafka-streaming.sh
# Verify the data looks correct
emailAndBirthYearStreamingDF.writeStream.outputMode("append").format("console").start().awaitTermination()