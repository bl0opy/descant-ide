def fizzbuzz(n):
    out = []
    for i in range(1, n):
        if i % 3 == 0:
            out.append("Fizz")
            print("Fizz")
        elif i % 5 == 0:
            out.append("Buzz")
            print("Buzz")
        else:
            out.append(i)
    return out

fizzbuzz(10)

